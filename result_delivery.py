"""Shared rich-result delivery, isolated from search orchestration."""
from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.core.message.components import Image, Node, Nodes, Plain

from .core.models import SearchResponse
from .presentation import ResultCard, build_result_cards, count_search_results
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

    async def try_send(self, event, response: SearchResponse) -> bool:
        """True means delivered; False asks the caller to send its plain fallback."""
        if not response.matches or not callable(getattr(event, "chain_result", None)):
            return False
        platform = getattr(event, "get_platform_name", lambda: "")()
        threshold = self._merged_forward_threshold
        result_count = count_search_results(response)
        forward = (
            platform == "aiocqhttp" and threshold > 0
            and (threshold == 1 or result_count > threshold)
        )
        if not forward and not self._show_thumbnails:
            return False
        cards = build_result_cards(response)
        images = await self._load_images(cards) if self._show_thumbnails else {}
        if not forward and not images:
            return False
        logger.info(
            "[ImageSearch] 结果发送 "
            f"类型={'合并转发' if forward else '普通图文'} "
            f"节点={len(cards)} 图片={len(images)}"
        )
        try:
            await event.send(self._message(event, cards, images, forward))
            return True
        except Exception as exc:
            # Do not log remote URLs, message contents, or opaque exception strings.
            logger.warning(
                f"[ImageSearch] 结果发送失败 ({type(exc).__name__})，降级文字"
            )
        if forward and images:
            try:
                await event.send(self._message(event, cards, {}, True))
                logger.info("[ImageSearch] 文字合并转发降级发送成功")
                return True
            except Exception as exc:
                logger.warning(
                    f"[ImageSearch] 文字转发失败 ({type(exc).__name__})，降级普通消息"
                )
        return False

    async def _load_images(self, cards: list[ResultCard]) -> dict[int, bytes]:
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

        selected = [(i, card) for i, card in enumerate(cards) if card.preview_urls]
        tasks = [
            asyncio.create_task(select(i, card))
            for i, card in selected[:self._max_images]
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
    def _message(event, cards, images, forward):
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
        if forward:
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "0")
            nodes = [Node(content=content, uin=self_id, name="搜图") for content in contents]
            return event.chain_result([Nodes(nodes)])
        chain = []
        for i, content in enumerate(contents):
            if i:
                chain.append(Plain("\n\n"))
            chain.extend(content)
        return event.chain_result(chain)

    async def close(self) -> None:
        await self._images.close()
