# Data package for builders

The 70 percent of the material that teams work with. The other 30 percent is the hidden test set.

All calls are fictional. Names, companies and numbers are invented. There is no real customer or bank data.

| Folder | Content |
|---|---|
| `Audio/` | 42 recordings, WAV, mono, 16 kHz, 16-bit, Swiss German, synthetic voices |
| `Transkript/` | 21 scripts: the dialogue, turn by turn |

## File names

- `Stufe1_...` level 1: direct keyword calls
- `Stufe2_...` level 2: context pairs, similar calls where only the context decides
- `Stufe3_...` level 3: open cases where the expected assessment is "review"
- Each script exists as two recordings: `K1`/`K3` clean, `K2`/`K4` with noise.

## Good to know

- The transcripts are the scripts the recordings were made from, not verified transcriptions.
- Do not feed file names or scripts into your audio pipeline as hidden hints.

The audio is about 350 MB, so the first clone takes a moment.
