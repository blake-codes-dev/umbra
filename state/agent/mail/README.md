# Agent mailbox

Two agents work on Umbra: one in a Linux container, one on the Windows box. They share a disk: the
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
3. **Git** - the durable record. `tools/mail.py` needs no commit, but decisions and results belong in
   `WORKLOG.md` and get pushed, so history stays complete even if the scratch tree is wiped.
