# Vid Studio — design rules

Dark theme in the spirit of apple.com. Everything below is what `worker/static/style.css` already does;
new screens follow it, and `worker/static/ui-audit.js` checks the rules marked **[A]** on every page
(how to run it: the last section).

## 1. Colours and type
- Colours come only from the tokens in `:root` (`--bg`, `--panel`, `--panel-2`, `--panel-3`, `--field`,
  `--line`, `--text`, `--muted`, `--blue`, `--link`, `--green`, `--amber`, `--err`, `--brand`). No new hex values in markup.
- `--brand` (pink-red → orange gradient) is the logo colour: logo, channel avatar, switches that are on,
  the active sidebar icon. It is not used for text or big areas.
- Blue (`--blue`) = the main action; green = done / healthy; amber = warning; red (`--err`) = error and
  destructive actions only.
- Font: system SF / Inter. Page title (h1) 40 px bold, tight letter-spacing; card title (h2) 20 px;
  body 14 px; small print 12 px. **[A]** No visible text under 11 px.
- Text stays readable on its background: normal text uses `--text`, secondary text `--muted`.

## 2. Layout and spacing
- Top bar 56 px, frosted. Sidebar 272 px on desktop; at ≤ 900 px it slides in from the ☰ button.
- Spacing steps: 4, 8, 12, 16, 20, 24, 32, 40 px. Cards have 24 px padding and an 18 px radius.
- **[A]** Cards and blocks stacked on a page never touch: at least 16 px between them (20 px is the default).
- **[A]** Cards in one grid row start at the same height.
- Grids use `minmax(min(Npx, 100%), 1fr)` so a column never forces the page wider than the screen.
- **[A]** No page scrolls sideways — at 1440 px and at 375 px (phone). Wide tables scroll inside their card.
- **[A]** Nothing sticks out of its card, and fields never overlap each other.

## 3. Controls
- **Buttons** are pills (`.btn`): `btn-primary` (one main action per block), `btn-outline` (secondary),
  `btn-text` (quiet), `btn-danger` (deletes / irreversible). A link that acts as a button gets `.btn` too.
  **[A]** The label is centred in the button. **[A]** Every clickable thing is at least 24 px high.
- **On/off** settings are switches, never tick boxes: every `input[type=checkbox]` is drawn as a switch
  (grey = off, logo gradient = on). The label sits to the right with 12 px between; it never says
  "on"/"off" itself. **[A]** checked by the audit.
- **Text fields** fill their column. **[A]** Multi-line fields stretch only downwards (`resize: vertical`) and
  never wider than their column.
- **Selects** use the custom chevron; **tabs** are a segmented control (`.tabs` / `.tab`).
- **Menus** (☰, Create) and **toasts** are frosted glass; **dialogs** are 24 px-radius sheets.

## 4. Safety
- Anything that deletes or cannot be undone asks first (`confirm(...)` with what exactly is lost).
- Deleting a whole channel needs the channel name typed exactly; the button stays disabled until it
  matches, then a final confirmation, and the server checks the name again.
- Never show secrets (tokens, keys) in the page; IDs only where they are needed (Users page).

## 5. Icons and words
- Sidebar and toolbar use thin line icons (inline SVG, `currentColor`, 1.7 px stroke); emoji only inside
  content and Telegram messages.
- Button labels are verbs ("Create", "Rename", "Delete channel"); English UI, short sentences.

## 6. Checking a page
Open the Studio, then in the browser console (or the Claude browser tool):

```js
(await import("/static/ui-audit.js")).audit()          // this page
(await import("/static/ui-audit.js")).auditAll()       // every page, one after another
```

It returns the rules a page breaks with the elements involved; an empty list means the page passes.
Check at desktop width and again at 375 px.
