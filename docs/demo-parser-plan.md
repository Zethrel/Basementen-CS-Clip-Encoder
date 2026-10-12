# Plan: our own CS2 demo parser (option C)

Status: plan only, nothing built. Written 12 October 2026.

Every format detail below was read from the source of two open-source CS2
parsers, not recalled from memory:
[demoinfocs-golang](https://github.com/markus-wa/demoinfocs-golang) (Go, MIT) and
[demoparser2](https://github.com/LaihoE/demoparser) (Rust, MIT). CS Demo Manager
(CSDM) uses the first through
[cs-demo-analyzer](https://github.com/akiver/cs-demo-analyzer). Where this plan
says *verify*, the answer is not known yet and has to come from a real demo.

## 1. Read this first: what C buys and what it does not

- **It replaces one step.** That step is reading the demo, which is
  `csdm analyze` plus `csdm json` in `export_match()`.
- **It does not replace recording.** `csdm video` prints "The demo must have
  been analyzed and be present in the database." So `csdm analyze` and the
  PostgreSQL service are still needed for every demo we record. Replacing
  recording would mean driving CS2 and HLAE directly. That is a separate,
  larger project and is out of scope here.
- **What it does buy:**
  - The highlight scoring gets its data without CSDM.
  - The demo list can show players and highlights without PostgreSQL running.
  - We own the parser and can fix it ourselves.
  - It is a good learning project.
- **What it costs:**
  - Maintenance after CS2 patches is ours.
  - In 2026 alone, demoinfocs-golang needed three fixes after CS2 updates:
    - "support for animgraph 2 demos" (10 Apr)
    - "PlayerButtonsStateUpdate not working since last CS2 update" (30 Aug)
    - "decrypt encrypted net-messages in CS2 demos" (11 Oct)
  - The outer container format (`PBDEMS2`) has been stable since CS2 launched.
    What changes is what is inside it.

The design below keeps the maintenance small by reading as little of the demo
as possible (Tier 1), and leaves the expensive part (Tier 2, entities) for when
it is proven necessary.

## 2. Decisions this plan makes

| Decision | Choice | Why |
| --- | --- | --- |
| Language | Python 3.10+, standard library only | Same as `clipenc.py`, and PyInstaller keeps building one exe. Snappy and protobuf are small enough to write ourselves (section 4). |
| Shape | One module, `cs2demo.py`, next to `clipenc.py`, with `test_cs2demo.py` (unittest) | Matches the repo's one-file-per-tool layout. |
| Scope | **Tier 1** (events and string tables only); Tier 2 (entities) only if Tier 1 fails validation | Entities are about 4,000 lines in demoparser2 (`sendtables.rs` 791, `decoder.rs` 950, `path_ops.rs` 433, `entities.rs` 440, `variants.rs` 845, `prop_controller.rs` 620) and are where most patch breakage lands. |
| Output | A dict in the shape `clipenc.py` already reads from CSDM's export: `mapName`, `players[].steamId/name/teamName`, `kills[].killerSteamId`, plus our own extra fields | `match_summary()`, `player_in_match()` and `kill_count()` keep working unchanged, so the parser can be swapped in behind `export_match()`. |
| Oracle | CSDM's own JSON exports, which Clip Encoder already writes to `<work>\json` | Gives free reference answers for every demo the user has already clipped. |
| Licence | The repo stays All Rights Reserved. Write from the format description. If any code is translated from the MIT parsers, keep their copyright notice in that file. | MIT allows reuse only with the notice. |

## 3. The format, layer by layer (Tier 1 reads only these)

### 3.1 File header
- Bytes 0–7: the stamp `PBDEMS2` plus NUL. `HL2DEMO` means a CS:GO demo, which
  we refuse with a clear message.
- Bytes 8–15: skipped. demoinfocs skips them without reading them.

### 3.2 Frames, repeated until `DEM_Stop`
- `cmd`: varint32.
  - `cmd & 64` (`DEM_IsCompressed`) means the payload is Snappy-compressed.
  - `cmd & ~64` is the command.
- `tick`: varint32. `0xFFFFFFFF` means pre-game; treat it as 0.
- `size`: varint32, followed by `size` bytes of payload, which is a protobuf
  message.
- **Unknown command → skip `size` bytes and carry on.** This is what lets the
  parser survive Valve adding commands. The current enum already has
  `DEM_AnimationData = 16`, `DEM_AnimationHeader = 17` and `DEM_Recovery = 18`.

`EDemoCommands`, from `demo.proto`:

| Value | Command | Tier 1 use |
| --- | --- | --- |
| 0 | Stop | End of the demo |
| 1 | FileHeader | `CDemoFileHeader`: map_name = 5, patch_version = 2, server_name = 3, client_name = 4, build_num = 13 |
| 2 | FileInfo | `CDemoFileInfo`: playback_time = 1 (float), playback_ticks = 2, playback_frames = 3 |
| 6 | StringTables | `CDemoStringTables` snapshot (3.5) |
| 7 | Packet | `CDemoPacket`: data = 3, a bitstream of net messages (3.3) |
| 8 | SignonPacket | Same as Packet |
| 13 | FullPacket | `CDemoFullPacket`: string_table = 1 (`CDemoStringTables`), packet = 2 (`CDemoPacket`) |
| 4, 5 | SendTables, ClassInfo | Tier 2 only |
| others | | Skip |

### 3.3 Net messages inside a packet (a bitstream)
- The bit order is least significant bit first. Both reference readers shift
  right and take the low bits.
- Repeat while more than 7 bits remain:
  - `type`: a "ubitvar". Read 6 bits into `v`. Then:
    - if `v & 48 == 16`: `v = (v & 15) | read(4) << 4`
    - if `v & 48 == 32`: `v = (v & 15) | read(8) << 4`
    - if `v & 48 == 48`: `v = (v & 15) | read(28) << 4`
  - `size`: varint32, read from the bitstream.
  - `size` bytes: protobuf. They are **not byte-aligned**, so they need a
    bit-shifted copy. Implementation hint (benchmark it): turn the slice into
    an `int` with `int.from_bytes(..., "little")`, shift it, and turn it back.
- **Order within one packet matters.** demoinfocs stable-sorts each packet's
  messages before handling them:
  - first: `net_Tick` (4), `svc_CreateStringTable` (44),
    `svc_UpdateStringTable` (45) and `net_SpawnGroup_Load`
  - then everything else
  - last: `svc_PacketEntities` (55)

  For us this means a player joining and their first event can arrive in the
  same packet, so the string tables must be applied first.
- Inside a full packet, skip `svc_UserCmds`. demoinfocs does, because full
  packets are snapshots.

Messages Tier 1 handles; everything else is skipped by its size:

| Id | Message | Why |
| --- | --- | --- |
| 44 | `svc_CreateStringTable` | Players (3.4) |
| 45 | `svc_UpdateStringTable` | Players joining or leaving |
| 205 | `GE_Source1LegacyGameEventList` | Event names and key names (3.6) |
| 207 | `GE_Source1LegacyGameEvent` | Kills, rounds, teams |
| 78 | `svc_EncryptedData` | Skip. In Valve matchmaking demos this carries chat, and decrypting it needs a key. Kills do not use it. |

### 3.4 String tables (only `userinfo` matters)
- **`CSVCMsg_CreateStringTable` fields:**
  - name = 1, num_entries = 2, user_data_fixed_size = 3, user_data_size = 4,
    user_data_size_bits = 5, flags = 6, string_data = 7,
    uncompressed_size = 8, data_compressed = 9, using_varint_bitcounts = 10
  - Table ids are assigned in creation order: the first table created is 0,
    and so on.
  - If `data_compressed` is set, Snappy-decode the whole `string_data` first.
- **`CSVCMsg_UpdateStringTable`:** table_id = 1, num_changed_entries = 2,
  string_data = 3. Parse it with the settings of the table it updates.
- **Entry loop** (`num_entries` or `num_changed_entries` times), starting with
  `index = -1` and an empty key history that holds at most 32 entries:
  1. Index: 1 bit. If set, `index += 1`. If not, `index = varint32 + 1`.
  2. Key: 1 bit "has key". If set:
     - Read 1 bit "use history".
       - If set, read `pos` (5 bits) and `len` (5 bits). The key is
         `history[pos][:len]` followed by a NUL-terminated string. If `pos`
         is past the end of the history, the key is the string alone.
       - If not set, the key is a NUL-terminated string.
     - Push the key onto the history, dropping the oldest above 32.
  3. Value: 1 bit "has value". If set:
     - If the table has fixed-size user data, read `user_data_size` bits.
     - Otherwise:
       - if `flags & 1`, read 1 bit "compressed"
       - the length in bytes is a ubitvar if `using_varint_bitcounts`,
         otherwise 17 bits
       - read that many bytes
     - If "compressed" was set, Snappy-decode the value.
- **The `userinfo` value is `CMsgPlayerInfo`:**
  - name = 1, xuid = 2 (fixed64, the SteamID64), userid = 3,
    steamid = 4 (fixed64), fakeplayer = 5 (bot), ishltv = 6
  - The entry **index is the player slot**.
  - An entry with xuid 0 and no name is an empty slot. On creation the table
    holds one entry per possible slot (up to 64).

### 3.5 String table snapshots
`CDemoStringTables` has tables = 1. Each table has table_name = 1 and
items = 2, and each item has str = 1 and data = 2.

For `userinfo`, `str` is the slot number as text and `data` is a
`CMsgPlayerInfo`. Apply it the same way as 3.4.

### 3.6 Game events
- **The list** (`CMsgSource1LegacyGameEventList`):
  - descriptors = 1
  - each descriptor has eventid = 1, name = 2, keys = 3
  - each key has type = 1, name = 2
- **An event** (`CMsgSource1LegacyGameEvent`):
  - event_name = 1, eventid = 2, keys = 3
  - each key has type = 1, val_string = 2, val_float = 3, val_long = 4,
    val_short = 5, val_byte = 6, val_bool = 7, val_uint64 = 8
  - **Keys are positional.** `event.keys[i]` belongs to `descriptor.keys[i]`.
- **The list can be missing.** demoinfocs ships fallback lists keyed by
  `patch_version`, as a workaround for the `sv_hibernate_when_empty` bug.
  - Our first version: stop with a clear error naming the demo.
  - If it happens on real demos: save the list from a good demo of the same
    `patch_version` as JSON and use it as the fallback.
- **Player references in events:** if `userid <= 0xFFFF`, then
  `slot = userid & 0xFF`. Look the slot up in `userinfo`.

### 3.7 Events Tier 1 uses

| Event | Keys we read | For |
| --- | --- | --- |
| `player_death` | userid, attacker, assister, weapon, headshot, penetrated, noscope, thrusmoke, attackerblind, assistedflash, distance, attacker_pawn | Kills and their flags |
| `round_start` | timelimit | Round boundaries |
| `round_end` | winner, reason, message | Round result |
| `round_officially_ended` | | End of the round, after the post-round walk-around |
| `begin_new_match`, `round_announce_match_start` | | End of warmup and restarts: reset the round count |
| `player_team` | userid, team, oldteam, disconnect, isbot, silent | Team membership |

Known gap: the reference parsers fall back to `attacker_pawn` when `attacker`
does not resolve, and resolving a pawn handle needs entities (Tier 2). Tier 1
counts these kills as "killer unresolved" and reports how many there were.

## 4. Building blocks we write ourselves

- **Varint and protobuf wire decoder.**
  - Decodes wire types 0 (varint), 1 (64-bit), 2 (length-delimited) and
    5 (32-bit) into `{field: [values]}`.
  - Typed accessors handle the few messages above: strings, bytes, int32,
    bool, float, fixed64.
  - Unknown fields are ignored. That is protobuf's own compatibility rule, and
    it is why new fields from Valve will not break us.
- **Snappy decompressor.** Raw Snappy only, not the framed format:
  - a varint uncompressed length, then tagged elements: literal, copy with a
    1-, 2- or 4-byte offset
  - check the output length against the preamble and fail loudly on a
    mismatch
- **Bit reader** over `bytes`: least significant bit first, with `bit`,
  `bits(n)`, `ubitvar`, `varint32`, `cstring` and `bytes(n)`. `bytes(n)` uses
  the bit-shifted slice copy from 3.3.

## 5. Milestones

Each milestone ends with its unit tests green, and is committed separately.

### M0: spike on real demos (the user runs it on their PC)
1. Implement header, frame loop and Snappy, plus
   `python cs2demo.py frames <demo>`. It prints counts per command, the
   compressed share and the time taken.
2. Run it on 5 or more of the user's demos (matchmaking, Premier, any FACEIT
   demos, a recent and an older one) and paste the output back.
3. **Answers needed before M3:**
   - Are packets Snappy-compressed in practice, and how long does Python
     take per demo?
   - Is the game event list present in every demo?
   - Do `player_team` events appear for every player, including after
     halftime (open question in 3.7)?

### M1: container and packets
- Net-message splitting and the message-order rule from 3.3.
- `python cs2demo.py messages <demo>` prints counts per message id.
- **Done when** every sample demo parses to `DEM_Stop` with no error, and the
  message counts look plausible: many `PacketEntities`, and some game events.

### M2: players
- String tables (3.4 and 3.5) and `userinfo`.
- `python cs2demo.py players <demo>`.
- **Done when** the steamId and name lists match CSDM's export for every sample
  demo. Names may differ only in characters CSDM itself alters, which needs
  verifying.

### M3: kills and rounds
- The game event list and the events in 3.7.
- Round numbering: count `round_start` after the last `begin_new_match` or
  `round_announce_match_start`.
- `python cs2demo.py match <demo> --json` prints the dict in the shape
  `clipenc.py` reads.
- **Done when**, compared with CSDM's export for each sample demo:
  - kill counts per player are equal
  - each kill's (round, killer, victim, headshot) matches
  - the number of rounds is equal
- Any mismatch is listed with its tick, so it can be looked up in CSDM's
  window.

### M4: teams and clutches (inferred)
- Teams come from `player_team`.
- A clutch is the last living player of a team, counted from `player_death`
  within the round, against N living opponents.
- Compare with CSDM's export (`teamName` per player, and CSDM's clutch list).
- **If teams or clutches do not match on the sample demos, stop.** Report the
  failure and decide on Tier 2 from that evidence.

### M5: wire it into Clip Encoder
- Settings and data flow:
  - A `parser` setting in `config.json` takes `"csdm"` (default, today's
    behaviour) or `"own"`.
  - With `"own"`, `export_match()` gets its data from `cs2demo`. The
    `csdm analyze` call stays, because recording needs it (section 1).
- `clipenc.py compare <demo>` runs both readers and prints any differences,
  for checking after CS2 patches.
- Highlight scoring (the reason for all this):
  - Score each round for the chosen player: kills in the round,
    1vX clutch won, headshots, noscope, wallbang (`penetrated > 0`),
    through smoke, killer blinded, long distance.
  - The weights live in `config.json`, because "best" is a matter of taste.
- Recording only the chosen rounds:
  - Use `csdm video --mode player --steamids <id> --event kills --rounds a,b,c`.
  - `--rounds` is in CSDM's current source (4 October 2026). **Verify** that
    3.20.1 has it with `csdm help video`.
- The window shows the ranked rounds, each with its reasons, for example
  "R14: 1v3 won, 3 kills, 2 HS".

### Tier 2 (only if M4 fails or entity data is wanted)
- What it involves:
  - `DEM_SendTables`, which holds `CSVCMsg_FlattenedSerializer`
  - `DEM_ClassInfo`
  - the `instancebaseline` string table
  - `svc_PacketEntities`, whose field paths are Huffman-coded
  - the field decoders: quantised floats, coords, angles and so on
- What it gives: authoritative teams, `m_totalRoundsPlayed`, pawn handles
  (fixing the known gap in 3.7), positions.
- Size: roughly the 4,000 lines of Rust in demoparser2. In Python, speed is
  the risk, so benchmark it before committing to it.
- Plan it separately once M0–M4 data exists.

## 6. Tests

- **Unit tests run without demo files.** The cloud sessions have no demos and
  no CS2. Each test builds its input with small test-only encoders: varint,
  protobuf fields, an LSB-first bit writer and Snappy literals. Cover:
  - varint edges: 5-byte values and `0xFFFFFFFF`
  - all four ubitvar branches
  - protobuf: unknown fields skipped, repeated fields
  - Snappy: literals and all three copy forms, overlapping copies, and a bad
    length that must fail loudly
  - string tables: increment and explicit index, key history (including
    `pos` past the end), fixed-size and variable-size values, compressed
    values
  - frames: an unknown command is skipped by its size, and a truncated file
    gives a clear error rather than a hang
  - the message order within a packet
  - events: keys mapped by position; userid to slot
- **Integration tests are opt-in** (`CS2DEMO_DIR` set): run M3's comparison
  over every demo in that folder that has a CSDM export. Demo files are never
  committed, because they are large and they hold other people's names and
  SteamIDs.

## 7. After a CS2 patch

1. Run `clipenc.py compare` on the first new demo. With `parser` set to
   `"csdm"` nothing changes for the user, so the own parser can lag behind
   safely.
2. Early warning: watch the commits of demoinfocs-golang and demoparser2.
   They usually fix breakage within days. We read their fix to learn what
   changed, and re-implement it.
3. Most patches add commands, messages or protobuf fields, which Tier 1
   already skips by design.

## 8. Open questions for the user

- Which demo sources are in use: Valve matchmaking or Premier, FACEIT, or
  others? This decides the sample set for M0.
- What speed is acceptable per demo, for example under 10 seconds? Measured
  in M0.
- Should `"own"` ever become the default, or always stay opt-in beside CSDM?
