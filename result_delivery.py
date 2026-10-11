"""Shared rich-result delivery, isolated from search orchestration."""
from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.core.message.components import Image, Node, Nodes, Plain

from .core.models import SearchResponse
from .presentation import BatchResult, ResultCard, build_batch_cards, build_result_cards, count_search_results
from .runtime.preview_images import PreviewImageLoader


class ResultDelivery:
    def __init__(
        self, *, merged_forward_threshold: int = 2, show_thumbnails: bool = True,
        max_preview_images: int = 6, proxy_url: str = "", user_agent: str = "",
    ) -> None:
        if (isinstance(merged_forward_threshold, bool)
                or not isinstance(merged_forward_threshold, int)
                or not 0 <= merged_forward_threshold <= 10):
            raise ValueError("merged_forward_threshold must be between zero and ten")
        self._merged_forward_threshold = merged_forward_threshold
        self._show_thumbnails = show_thumbnails
        self._max_images = max(1, min(20, max_preview_images))
        self._images = PreviewImageLoader(proxy_url=proxy_url, user_agent=user_agent)

    async def try_send(self, event, response: SearchResponse, *, log_context: str = "", cached: bool = False) -> bool:
        """True means delivered; False asks the caller to send its plain fallback."""
        return await self._try_send(event, build_result_cards(response, cached=cached), count_search_results(response), log_context,
                                    force_forward=count_search_results(response) >= 2)

    async def try_send_batch(self, event, results: list[BatchResult], *, log_context: str = "") -> bool:
        count = sum(count_search_results(item.response) for item in results if item.response is not None)
        groups = [build_batch_cards([item]) for item in results]
        cards = [card for group in groups for card in group]
        return await self._try_send(event, cards, count, log_context, [len(group) for group in groups],
                                    force_forward=any(count_search_results(item.response) >= 2
                                                      for item in results if item.response is not None))

    async def _try_send(self, event, cards: list[ResultCard], result_count: int, log_context: str,
                        group_sizes: list[int] | None = None, *, force_forward: bool = False) -> bool:
        if not cards or not callable(getattr(event, "chain_result", None)):
            return False
        group_sizes = group_sizes or [len(cards)]
        message_count = len(group_sizes)
        platform = getattr(event, "get_platform_name", lambda: "")()
        threshold = self._merged_forward_threshold
        forward = (
            platform == "aiocqhttp"
            and (force_forward or (threshold > 0 and message_count >= threshold))
        )
        if not forward and not self._show_thumbnails:
            return False
        images = await self._load_images(cards, group_sizes) if self._show_thumbnails else {}
        if not forward and not images:
            return False
        logger.info(
            f"[ImageSearch] {log_context} 结果发送 "
            f"类型={'合并转发' if forward else '普通图文'} "
            f"结果={result_count} 普通消息={message_count} 门槛={threshold} 卡片={len(cards)} 缩略图={len(images)} "
            f"消息={1 if forward else message_count} "
            f"同图多结果强制转发={'是' if forward and force_forward else '否'}"
        )
        if not forward and len(group_sizes) > 1:
            offset = 0
            for size in group_sizes:
                group = cards[offset:offset + size]
                previews = {i - offset: data for i, data in images.items()
                            if offset <= i < offset + size}
                try:
                    await event.send(self._message(event, group, previews, False))
                except Exception as exc:
                    logger.warning(
                        f"[ImageSearch] {log_context} 单图结果发送失败 ({type(exc).__name__})，降级文字"
                    )
                    # Retry only this image; previously delivered images must not repeat.
                    await event.send(event.plain_result("\n\n".join(card.text for card in group)))
                offset += size
            return True
        try:
            await event.send(self._message(event, cards, images, forward, group_sizes))
            return True
        except Exception as exc:
            # Do not log remote URLs, message contents, or opaque exception strings.
            logger.warning(
                f"[ImageSearch] {log_context} 结果发送失败 ({type(exc).__name__})，降级文字"
            )
        if forward and images:
            try:
                await event.send(self._message(event, cards, {}, True, group_sizes))
                logger.info(f"[ImageSearch] {log_context} 文字合并转发降级发送成功")
                return True
            except Exception as exc:
                logger.warning(
                    f"[ImageSearch] {log_context} 文字转发失败 ({type(exc).__name__})，降级普通消息"
                )
        return False

    async def _load_images(self, cards: list[ResultCard], group_sizes: list[int] | None = None) -> dict[int, bytes]:
        semaphore = asyncio.Semaphore(3)
        # Request-local cache: no disk residue and no duplicate downloads.
        downloads: dict[str, asyncio.Task] = {}

        async def download(url: str) -> bytes | None:
            async with semaphore:
                return await self._images.load(url)

        async def select(index: int, card: ResultCard):
            for url in card.preview_urls:
                if url not in downloads:
                    downloads[url] = asyncio.create_task(download(url))
                data = await downloads[url]
                if data:
                    return index, data
            return index, None

        selected = []
        offset = 0
        for size in group_sizes or [len(cards)]:
            candidates = [(offset + i, card) for i, card in enumerate(cards[offset:offset + size])
                          if card.preview_urls]
            selected.extend(candidates[:self._max_images])
            offset += size
        tasks = [
            asyncio.create_task(select(i, card))
            for i, card in selected
        ]
        if not tasks:
            return {}
        try:
            done, _ = await asyncio.wait(tasks, timeout=10)
            results = {}
            for task in done:
                try:
                    index, data = task.result()
                    if data:
                        results[index] = data
                except Exception:
                    pass  # A thumbnail must never discard a search result.
            return results
        finally:
            all_tasks = tasks + list(downloads.values())
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

    @staticmethod
    def _message(event, cards, images, forward, group_sizes=None):
        contents = []
        for i, card in enumerate(cards):
            if i not in images:
                contents.append([Plain(card.text)])
                continue
            image = Image.fromBytes(images[i])
            if card.image_after_lines is None:
                contents.append([Plain(card.text), image])
                continue
            lines = card.text.split("\n")
            before = "\n".join(lines[:card.image_after_lines])
            after = "\n".join(lines[card.image_after_lines:])
            content = ([Plain(before)] if before else []) + [image]
            if after:
                content.append(Plain("\n" + after))
            contents.append(content)
        groups = []
        offset = 0
        for size in group_sizes or [len(cards)]:
            chain = []
            for i, content in enumerate(contents[offset:offset + size]):
                if i and content and isinstance(content[0], Plain):
                    # Attach separators to visible text; adapters may discard blank components.
                    content = [Plain("\n\n" + content[0].text), *content[1:]]
                chain.extend(content)
            groups.append(chain)
            offset += size
        if forward:
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "0")
            nodes = [Node(content=content, uin=self_id, name="搜图") for content in groups]
            return event.chain_result([Nodes(nodes)])
        return event.chain_result([item for group in groups for item in group])

    async def close(self) -> None:
        await self._images.close()
