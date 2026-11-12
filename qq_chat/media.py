"""Receiving what people send besides text: file attachments (owner, private chat) and images."""
import asyncio
from dataclasses import replace
import logging

from . import prompts
from .files import MAX_FILE_BYTES, save_attachment
from .images import MAX_IMAGE_BYTES, save_image, save_reference_image
from .message_policy import image_request

REFERENCE_IMAGE_BUDGET = 15
REFERENCE_REFRESH_TIMEOUT = 5


class Media:
    """Needs self.cfg and self.onebot_call()."""

    async def receive_attachments(self, incoming):
        if incoming.source.kind != 'private' or str(incoming.source.sender_id) != str(self.cfg['owner_qq']):
            raise ValueError('当前聊天未开放附件接收')
        if len(incoming.attachments) > 5:
            raise ValueError('单条消息最多接收 5 个附件')
        paths = []
        for attachment in incoming.attachments:
            file_id = attachment.get('file_id')
            if not isinstance(file_id, str) or not file_id:
                raise ValueError('附件缺少 file_id')
            if int(attachment.get('file_size') or 0) > MAX_FILE_BYTES:
                raise ValueError('附件超过 50 MiB 上限')
            result = await self.onebot_call('get_file', {'file_id': file_id})
            path = await asyncio.to_thread(save_attachment, result.get('file', ''),
                                          result.get('file_name') or attachment.get('file') or 'attachment')
            paths.append(str(path))
            logging.info('Private attachment saved: %s', path)
        return prompts.attachments_saved(incoming.text, paths)

    async def receive_images(self, incoming, *, prefer_url=False):
        if not image_request(incoming.source, self.cfg['owner_qq']):
            raise ValueError('当前聊天未开放图片接收')
        if len(incoming.images) > 5:
            raise ValueError('单条消息最多接收 5 张图片')
        paths = []
        for image in incoming.images:
            if int(image.get('file_size') or 0) > MAX_IMAGE_BYTES:
                raise ValueError('单张图片超过 20 MiB 上限')
            url = image.get('url', '')
            if prefer_url and url:
                try:
                    path = await save_reference_image('', url)
                    paths.append(str(path))
                    continue
                except (ValueError, OSError, asyncio.TimeoutError):
                    # Forwarded nodes may not exist in the client's message DB,
                    # even when their signed CDN addresses are still readable.
                    pass
            result = {}
            ident = image.get('file_id') or image.get('file')
            if ident:
                try:
                    if prefer_url:
                        result = await asyncio.wait_for(
                            self.onebot_call('get_image', {'file': ident}),
                            REFERENCE_REFRESH_TIMEOUT)
                    else:
                        result = await self.onebot_call('get_image', {'file': ident})
                except (ValueError, asyncio.TimeoutError):
                    # Fresh event URLs can still work when the image cache lookup fails.
                    pass
            if prefer_url:
                # The event URL has already failed; only retry a refreshed URL
                # or local cache instead of fetching the same address twice.
                refreshed_url = result.get('url', '')
                path = await save_reference_image(
                    result.get('file', ''), refreshed_url if refreshed_url != url else '')
            else:
                path = await asyncio.to_thread(save_image, result.get('file', ''),
                                               result.get('url') or url)
            paths.append(str(path))
        return paths

    async def receive_reference_images(self, incoming, images, records):
        """Images inside a quoted or forwarded message: one shared time budget; each outcome is noted in records."""
        saved = ()
        deadline = asyncio.get_running_loop().time() + REFERENCE_IMAGE_BUDGET
        for number, image in enumerate(images, 1):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                records.append({'kind': '提示', 'text': f'参考图片处理已超时（第{number}张），未读取图片内容。'})
                continue
            try:
                paths = await asyncio.wait_for(
                    self.receive_images(replace(incoming, images=(image,)), prefer_url=True),
                    remaining)
                saved += tuple(paths)
                records.append({'kind': '参考图片读取结果', 'text': f'参考图片{number}对应文件：' + ', '.join(paths)})
            except (ValueError, OSError, asyncio.TimeoutError):
                records.append({'kind': '提示', 'text': f'参考图片获取失败（第{number}张），未读取图片内容。'})
        return saved
