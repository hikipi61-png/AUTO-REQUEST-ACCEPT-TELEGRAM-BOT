# Auto Request Approve Bot

A focused Telegram bot for fast pending join-request approval. It sends the
branded request message first, then retries `approveChatJoinRequest` until the
request is accepted or Telegram's temporary request window expires.

The public commands are intentionally limited:

- `/start` — copies the saved channel post (if configured), then sends the
  Raj welcome message with Add to Group, Add to Channel, `/approve` help, and
  Info Bot buttons.
- `/help` — branded setup instructions and the “how to add bot” image.
- `/approve` — opens the same setup instructions.
- `/raj` — admin panel.

## Included assets

- `assets/raj-bots.png` — default request/help branding image. It is uploaded
  to MongoDB GridFS on startup and reused from there.
- `assets/how-to-add-bot.jpg` — the supplied Telegram “Add to Group or
  Channel” screenshot. It is also saved to MongoDB GridFS.

The default request message uses `raj-bots.png` and places the Info Bot button
above the small-caps **ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ** button:
`https://t.me/+A6klPh9Ms-MwYjBl`.

## Setup

1. Create a bot with `@BotFather` and copy its token.
2. Create a MongoDB database with a URI that allows the bot to use normal
   collections and GridFS.
3. Set the required values as environment secrets:

   ```bash
   export BOT_TOKEN="your-token"
   export MONGO_URI="mongodb+srv://..."
   ```

4. Install and run:

   ```bash
   python3 -m pip install -r requirements.txt
   python3 bot.py
   ```

   On Replit, keep `BOT_TOKEN` and `MONGO_URI` in Secrets. Do not put either
   value in this folder or in a ZIP.

## First-time Telegram setup

1. Open the bot profile and choose **Add to Group or Channel**.
2. Add it to the target group/channel.
3. Promote it to administrator with permission to invite users/manage join
   requests.
4. Create an invite link with **Approve New Members** enabled.
5. Open the bot in private chat and send `/raj`.
6. Open **My Chats**, select the target chat, and confirm **Auto Mode ON**.
7. Optional: use **Set Start Post** and forward a channel post. It will be
   copied as the first `/start` message.

## MongoDB collections

The bot creates `users`, `chats`, `join_events`, `approval_stats`, `settings`,
`admins`, and GridFS collections. The two uploaded images are seeded by SHA-256
hash, so restarts do not create duplicate media files.

## Notes about the fast approval flow

Telegram gives bots a short-lived private-chat identifier on a join-request
update. The bot sends the request message before approval, then uses a bounded
retry loop with Telegram's `retry_after` value when available. It stops cleanly
when the request is already processed, the bot loses permissions, or Telegram
expires the request.

The bot cannot override Telegram permissions, flood limits, expired requests,
or a user's privacy restrictions. Keep the bot administrator in the target
chat and use an approval invite link.
