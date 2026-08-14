# Dashboard UI parity

This matrix compares the last validated dashboard (`76bd23b`) with the daily
dashboard introduced in `5422057`. It is a restoration checklist: user-facing
behaviour may be reorganised, but it must not disappear.

| Feature | Pre-refactor | Daily dashboard | Restoration target |
| --- | --- | --- | --- |
| Device Camera starts on opening the Camera tab | Yes | Regressed | Restore validated viewer/WHEP lifecycle |
| Home Live link | Yes | Regressed through Camera gate | Opens an auto-starting Camera tab |
| Schedule create/edit/delete | Yes | Present | Preserve ACK-backed workflow |
| Schedule disable | Yes | Missing | Restore with `repeatDay: []` |
| Schedule enable | Edit-only | Missing | Provide explicit enable action with safe daily repeat default |
| All typed feeder settings | Eight groups | Regressed to four toggles | Restore all existing typed routes |
| Settings drafts during polling | Yes | Present but incomplete | Preserve all active form drafts and focus |
| Advanced diagnostics | State/log tabs | Incomplete | Keep redacted device diagnostics behind persistent Advanced mode |
| Manual dispense | No UI or typed route | ACK-confirmed typed action | Local-only request; feeder ACK forwards naturally to cloud |

The relay deliberately treats `MANUAL_FEEDING_SERVICE` as unsafe for automatic
replay. The dashboard action is therefore a dedicated ACK-confirmed, non-queued
control: it publishes only to the local feeder and succeeds only after its
natural `/service/post` acknowledgement with `code=0`.
