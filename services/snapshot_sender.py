"""Send the exact version that was assessed; copy_message can copy a later unreviewed edit."""

async def send_snapshot(bot, message, chat_id, thread_id=None, reply_parameters=None):
    common = dict(chat_id=chat_id, message_thread_id=thread_id, reply_parameters=reply_parameters)
    caption = dict(caption=message.caption, caption_entities=message.caption_entities)
    if message.text is not None:
        return await bot.send_message(text=message.text, entities=message.entities,
                                      link_preview_options=message.link_preview_options, **common)
    if message.photo:
        return await bot.send_photo(photo=message.photo[-1].file_id,
                                    has_spoiler=message.has_media_spoiler,
                                    show_caption_above_media=message.show_caption_above_media,
                                    **caption, **common)
    for kind in ('video', 'animation', 'audio', 'document', 'voice'):
        media = getattr(message, kind, None)
        if media:
            return await getattr(bot, 'send_' + kind)(**{kind: media.file_id}, **caption, **common)
    if message.video_note:
        return await bot.send_video_note(video_note=message.video_note.file_id, **common)
    if message.sticker:
        return await bot.send_sticker(sticker=message.sticker.file_id, **common)
    if message.contact:
        contact = message.contact
        return await bot.send_contact(phone_number=contact.phone_number, first_name=contact.first_name,
                                      last_name=contact.last_name, vcard=contact.vcard, **common)
    if message.venue:
        venue = message.venue
        return await bot.send_venue(latitude=venue.location.latitude, longitude=venue.location.longitude,
                                    title=venue.title, address=venue.address, **common)
    if message.location:
        return await bot.send_location(latitude=message.location.latitude,
                                       longitude=message.location.longitude, **common)
    if message.poll:
        poll = message.poll
        if poll.type == 'quiz':
            raise ValueError('无法从快照恢复测验的隐藏正确答案')
        return await bot.send_poll(question=poll.question, options=[opt.text for opt in poll.options],
                                   is_anonymous=poll.is_anonymous,
                                   allows_multiple_answers=poll.allows_multiple_answers, **common)
    raise ValueError('该消息类型无法安全地从审核快照恢复，请让用户改发文本或普通媒体')
