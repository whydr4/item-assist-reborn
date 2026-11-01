# Quick-buy event in client 6839

Inspected the installed Windows client on 2026-11-01 (`SourceRevision=11193905`).
`client.dll` SHA-256: `929a0455e510ad8bb76c26737a9c84a58ed496267b89b243f94b0699bb10855a`.

`CitadelQuickbuyAddItem` registration at VA `0x180092d40` declares four parameters
(`r8d = 4`). Parameter metadata at `0x180607590` supplies:

1. `CUtlStringToken` (unsigned item ID)
2. `bool`
3. `bool`
4. `EItemPurchaseSource`

The previous mod call supplied three numbers. The current call is
`$.DispatchEvent("CitadelQuickbuyAddItem", Number(id), false, false, 0)`.
The numeric source value and flag semantics still require in-game verification;
the regression test verifies argument count, types, and preservation of high-bit item IDs.

The diagnostic session before this change reported `autoQueue=false`, `autoBuy=true`,
and repeated `queue_result` events with an empty `have` list. This narrows the issue
to queue population/observation rather than the mod toggle or auto-buy toggle.
A populated `queue_result.have` in a fresh session is the acceptance check.

The exact RTTI event type is
`.?AV?$CUIEvent@$0A@VCUtlStringToken@@_N_NW4EItemPurchaseSource@@@panorama@@`.
This confirms the two boolean parameters precede the enum. In the metadata constructor,
`bool` is stored at stack offsets `0x30` and `0x38`, with the enum at `0x40`.
The first four-argument attempt placed the enum second; the next game log explicitly
reported `Event arguments could not be parsed` for that incorrect order.

Follow-up: both four-argument forms failed in-game. Inspection of the actual text parser
at `0x180601700` shows it parses an integer and two booleans but returns null on all
paths (the output is zeroed at `0x18060177e`). Native type metadata therefore does not
establish that this event can be constructed through JavaScript DispatchEvent.

Current transport: `CitadelConCommand` carrying `quickbuy add <unsigned-id> 0 false 0`.
This is the command format emitted by the native controller at `0x180604a89`.
Clear uses `quickbuy clear` through the same transport. Command dispatch and queue
contents are logged separately; absence of an exception is not proof of population.

Localization regression tests load the complete generated JS, including appended
`QOL_REC_DATA.loc` assignments, and resolve all six diagnostic queue items in both
English and Russian to identical IDs.

In-game confirmation: command transport populated all six queue entries. The observed order
was the reverse of `want` when sent in reverse order, confirming that the command appends.
The rebuild now sends recommendations first-to-last. Regression coverage verifies both
append order and that a matching queue is left intact on the next sync.
