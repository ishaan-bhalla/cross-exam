# Design

Art direction for the web app (`src/crossexam/web/`). Plain HTML, CSS and JS served by FastAPI: no build step, no animation libraries, self-hosted fonts.

## Brief

- **Who:** lawyers, law students and Inquiry watchers practising cross-examination.
- **Job:** get a witness on screen fast (the example witness, or an uploaded statement), check what the app drafted about them, then question them.
- **Tone:** a professional legal tool. Calm, readable and high contrast, in light and dark.

## Concept card

```
Idea:        The witness answers only from their statement, and you watch the words being found in it.
Metaphor:    The hearing bundle: numbered paragraphs, page references, a highlighter.
Signature:   The highlighter band. On the front page it sweeps word by word across a real quoted
             passage (Ismay ¶41), exactly as it does during an examination. Everywhere else it
             marks only what the witness is relying on.
Emotion:     Orderly, quiet confidence: a well-prepared bundle on the desk.
Motion verb: "Marked like a highlighter": left to right, word by word, then it stays.
Screenshot:  The specimen: counsel's question, the paragraph with its margin number, the band.
```

The concept was picked over two alternatives:
- "A transcript page", with line numbers and a court-reporter layout. It was too close to a document viewer, and the statement, not the transcript, is the source of truth.
- "Exhibit tabs", with coloured tab dividers. That needs more colours than a calm tool should carry.

The highlighter is the product's own mechanism, so this is the one choice that couldn't be moved to another app.

## System

**Palette:**

| Role | Light | Dark |
|---|---|---|
| ground (paper) | `#f5f6f4` | `#111315` |
| surface | `#ffffff` | `#191c1f` |
| ink | `#14171a` | `#eceef0` |
| muted | `#4f5862` | `#a7b0ba` |
| line | `#d3d8dd` | `#2f353b` |
| accent (counsel navy: buttons, links, focus) | `#1f3d63` | `#9cc0ff` |
| band (highlighter) | `#f6d64a` | `#7a5f0e`, with light ink on it |
| warn | `#8a2c0d` | `#ffb38a` |

- The ground is a cool paper grey, not cream.
- Yellow is used only for the band, never for decoration.

**Type:**
- **Newsreader**, a variable serif with optical sizes, carries the statement and the headings. Statements are long reading, and a text serif with optical sizing reads well at 17–18 px.
- **IBM Plex Sans** carries the interface: labels, buttons and badges. Its tabular figures are used for paragraph numbers.
- Body text is 17 px with 1.55 leading. Display text is set in Newsreader at the largest optical size, with tracking of −0.02em.

**Grid:**
- **Front page:** 12 columns with a 24 px gutter. The text runs over columns 1–6 and the specimen over 7–12; on a phone, everything stacks.
- **Confirm page:** a form column of 5 and a statement preview of 7.
- **Examination:** two equal panes, as before.

**Shape:** 4 px radius on every control and panel, and 2 px on the band. Panels have no shadows; lines separate them.

**Motion:**
- One easing, `cubic-bezier(.2,.7,.2,1)`, over 160–240 ms for controls and 90 ms per word for the specimen band.
- Primary buttons answer by filling the band colour along their bottom edge, left to right: the motion verb.
- Under reduced motion everything is static, and the specimen shows its band already complete.

**Idle state:** still, by decision. This is a work tool; after the one-time sweep, nothing moves while you read.

**Genericness test:**
- I swapped a centred hero for a left-aligned statement and a real specimen.
- I left out feature cards. There are exactly two choices, set as unequal panels: the upload panel is larger, because it's the main action.
- I used no gradient and no glass.
