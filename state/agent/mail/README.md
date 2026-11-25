# Agent mailbox

Two agents work on Lyphen: one in a Linux container, one on the Windows box. They share a disk: the
container mounts `C:\` at `/config` over 9p, so **a file written here is visible on Windows
immediately** at the same path. No sync, no network, no git needed for the handshake.

## Files

| file | written by | read by |
| --- | --- | --- |
| `to-windows.md` | container | Windows |
| `to-container.md` | Windows | container |
| `.cursor-<side>.json` | the reader's tool | - |

Messages are **append-only**: never rewrite or reorder, just add. Each message starts with a
`## <timestamp> · <sender>` heading, so the tools can split them and keep a read cursor.

## Commands

Container (bash):

```bash
python3 tools/mail.py status
python3 tools/mail.py read --for container      # unread, advances the cursor
python3 tools/mail.py send --to windows "core.dll rebuilt, run the harness"
```

Windows (cmd, from the repo root):

```bat
tools\mail.bat status
tools\mail.bat read
tools\mail.bat send "harness reproduces natively, rsp is off by 8"
```

`watch` blocks and prints messages as they land, for a long session:

```bash
python3 tools/mail.py watch --for container
```

## Channels, and what each is for

1. **This mailbox** - the default. Instant, offline, plain text, easy to paste into a log.
2. **`#relay` on Discord** (`python3 tools/relay.py send|tail|watch`, channel `1575012687878950992`)
   - for "look now" pings and for the owner to follow along from anywhere. Needs the bot token.
3. **The bridge** (`tools/relay_daemon.py`, running as pid in `state/agent/relay.pid`) makes those
   two into one channel, so nobody has to care which one the other used:

   ```
   a #relay post tagged [windows]      ->  appended to to-container.md
   a new message in to-windows.md      ->  echoed to #relay as [container]
   /say <text> typed in #relay by a human ->  appended to to-container.md
   ```

   Control it with `bash tools/relay_ctl.sh start|stop|status|logs`.

   Long echoes are cut at ~1900 characters and say so, pointing at `to-windows.md` for the rest;
   the `/say` reply does the same instead of leaving the sender wondering. Nothing is lost - the
   mailbox always holds the whole text.

   The bridge is a convenience: it lives in this scratch tree, so it dies if the app restarts and
   is simply restarted with `relay_ctl.sh start`. The Windows agent never needs it - it has the
   shared disk, and mail is the same file either way.

   Caveat worth knowing: without the privileged **Message Content** intent the bot cannot read the
   text of messages other users type, only its own - `GET /messages` returns `content: ""` for them.
   Interactions are exempt, which is why `/say` is the human's way in. Enabling the intent in the
   Developer Portal would make plain messages work too, but nothing here depends on it.

4. **Git** - the durable record. `tools/mail.py` needs no commit, but decisions and results belong in
   `WORKLOG.md` and get pushed, so history stays complete even if the scratch tree is wiped.
