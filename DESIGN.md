---
name: Camera Monitor
description: Traditional Chinese camera monitoring with a restrained dark utility interface.
colors:
  bg: "#101216"
  surface: "#191e26"
  raised: "#232a35"
  line: "#343d4a"
  text: "#edf0f5"
  muted: "#a8b2c1"
  accent: "#91baff"
  action: "#294d80"
  green: "#7eddb4"
  red: "#ff989f"
  amber: "#e5c489"
typography:
  headline:
    fontFamily: 'system-ui, -apple-system, "Noto Sans TC", sans-serif'
    fontSize: "1.65rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.025em"
  dialog-title:
    fontSize: "1.25rem"
    fontWeight: 600
  title:
    fontSize: "1.15rem"
    fontWeight: 600
  section:
    fontSize: "1rem"
    fontWeight: 600
  body:
    fontFamily: 'system-ui, -apple-system, "Noto Sans TC", sans-serif'
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontSize: "0.9rem"
  metadata:
    fontSize: "0.86rem"
  media-time:
    fontSize: "0.95rem"
    fontWeight: 550
rounded:
  badge: "5px"
  selection: "7px"
  field: "8px"
  button: "9px"
  media: "12px"
  viewer: "14px"
  dialog: "16px"
spacing:
  compact: "8px"
  control: "10px"
  small: "12px"
  heading: "14px"
  medium: "16px"
  gutter-small: "18px"
  section: "20px"
  grid-row: "22px"
  dialog: "24px"
  page-top: "30px"
  gutter: "32px"
components:
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.text}"
    rounded: "{rounded.button}"
    padding: "9px 15px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.button}"
    padding: "9px 15px"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.button}"
    padding: "9px 15px"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.red}"
    rounded: "{rounded.button}"
    padding: "9px 15px"
  date-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.field}"
    padding: "8px 12px"
  nav-current:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.field}"
    padding: "9px 16px"
  media-frame:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.muted}"
    rounded: "{rounded.media}"
    padding: "0"
    width: "100%"
  dialog:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.dialog}"
    padding: "0"
  toast:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.media}"
    padding: "10px 12px 10px 18px"
---

# Design System: Camera Monitor

## Overview

**Creative North Star: "Existing dark utility interface"**

This is the confirmed incumbent identity, recorded from the finished application rather than a new metaphor. Blue-gray grounds, understated blue actions, neutral dividers and semantic green/red states support a compact Traditional Chinese monitoring tool. The media supplies the visual weight; controls remain plain and legible.

The shared system covers live.html, snapshots.html and recordings.html. It uses one header, three-route navigation and a native settings dialog. It preserves the existing product's practical voice without adding a marketing or display identity.

**Key Characteristics:**
- Dark blue-gray tonal surfaces with restrained blue actions.
- One system sans family, compact headings and tabular media times.
- Open media grids, softly rounded controls and thin dividers.
- Explicit text alongside semantic state and reversible-action feedback.

Source authority: camera-ui.css supplies all visual tokens and responsive rules; camera-ui.js supplies navigation, SVG geometry, settings and notifications; camera-library.js supplies media, preview, selection and recovery states. The three current HTML routes share these files. PRODUCT.md and .impeccable/surfaces/live-html.md confirm the incumbent identity. Legacy motion.html is outside this shared system. Sidecar tonal ramps are synthesized preview metadata, not additional shipped colors.

## Colors

The palette is a cool dark ground with pale text, subdued blue action fills and brighter blue focus indicators. Frontmatter values preserve the source custom-property names and are normative.

### Primary
- **Understated Action Blue** (`action`): filled recording and save actions.
- **Pale Focus Blue** (`accent`): keyboard outlines, selected-media outlines, brand camera and undo text.

### Neutral
- **Blue-Black Ground** (`bg`): body and header.
- **Dark Blue-Gray Surface** (`surface`): fields, media frames, dialogs and skeletons.
- **Raised Blue-Gray** (`raised`): current navigation, control hover and notifications.
- **Slate Divider** (`line`): control borders, section dividers and audio meter track.
- **Pale Reading Text** (`text`): titles, controls and primary content.
- **Muted Slate Text** (`muted`): supporting copy, inactive navigation and metadata.

Semantic states use `green` for healthy connection and audio level, `red` for recording/error/destructive actions, and `amber` for notices. Preserve accompanying labels; the colored dot is supplementary. Interaction-only border, hover and pressed colors remain in the source and sidecar snippets rather than a fabricated palette scale.

**The Labeled State Rule.** Pair connection and recording colors with visible text; color alone does not identify the current state.

## Typography

**Body and heading font:** the system sans stack in frontmatter. The root is (15px); rem values are relative to it. There is no separate display face or display role.

### Hierarchy
- **Headline:** page title, tightening to (1.4rem) at the phone breakpoint. Its compact tracking and line height are explicit in frontmatter.
- **Dialog title:** settings and preview headings.
- **Title:** secondary headings, with operational recording/audio headings using the smaller section role.
- **Section:** settings subsections and compact operational headings.
- **Body:** controls and primary prose, inheriting the body's relaxed line height.
- **Label / Metadata:** connection labels, field labels and compact supporting copy.
- **Media time:** medium-weight time label with tabular numerals; counts, durations and output values also use tabular numerals.

Date headings use (1.02rem), durations (0.76rem), and file sizes (0.78rem). These are specialized metadata treatments, not a new display scale. Icons are inline SVG with rounded strokes, normally (20px) and stroke width (1.7).

## Layout

The centered header and main container cap at (1360px); live main caps at (1280px). Desktop main padding is (30px 32px 56px), and the header has a minimum height of (76px). The layout uses flexible rows and a responsive grid, with no fixed sidebar. Spacing is an observed mixed utility rhythm, not a uniform mathematical scale.

Media grids use four equal columns with row/column gaps (22px / 18px). At maximum width (1050px) they use three columns. At maximum width (700px) they use two columns with gaps (18px / 12px). Date groups have (32px) separation. Thumbnails are (16 / 10) on larger screens and (4 / 3) on phones; the live viewer is (16 / 9), with contained imagery by default and a user-selectable fill preference.

At the phone breakpoint, main padding becomes (24px 18px 40px). The header wraps: brand/settings stay above a full-width three-link navigation row. Secondary button wording and visible date labels compact while accessible names remain. The audio meter and decorative audio icon hide; the listening action stays available. Media action buttons shrink to (30px) width and (34px) minimum height, while standard buttons/fields retain their desktop (42px) minimum height. Viewer tools use (36px) on phones. These are distinct source sizes, not a claim that every control is 42px.

Dialogs retain viewport gutters via width `min(600px, calc(100% - 24px))` and maximum height `calc(100dvh - 32px)`. Confirmations cap at (420px), previews at (1100px). Preview media uses `object-fit: contain` and maximum height (72dvh).

## Elevation & Depth

Default depth comes from tonal surfaces and thin borders. Resting navigation and media items have no card shadow. Shadows are limited to modal dialogs (`0 24px 80px #0009`) and notifications (`0 10px 40px #0008`); the dialog backdrop is `#06090dbc`.

**The Overlay Depth Rule.** Reserve the shipped large shadows for modal dialogs and transient notifications; ordinary content uses tonal surfaces and dividers.

## Shapes

Controls are gently rounded: the frontmatter distinguishes field/navigation, button, media, viewer and dialog corners. Media frames clip their content; live full-screen removes its corner radius. Status dots are circular (7px). Badges use the smaller badge radius, while the selection checkbox container uses its own selection radius. Borders are normally (1px), with accent outlines for keyboard focus and selection.

## Components

### Buttons

Plain text or SVG-and-text actions use a (42px) minimum height, the button radius and frontmatter padding. Secondary buttons have a neutral outline. Primary buttons use Action Blue with border `#456da4` and hover fill `#355f98`; generic hover borders become `#69788c`. Generic pressed fill is `#303b4c`. Quiet buttons remove the resting border and use muted text. Danger buttons use red text and border `#764249`, with hover fill `#3e252b`. Disabled actions reduce opacity to (.45) and use a not-allowed cursor.

Every keyboard-focusable control receives a (2px) accent outline with (4px) offset. Selected media uses a separate (2px) accent outline with (3px) offset. There is no generic button transition or decorative hover movement.

### Inputs / Fields

Native selects and text fields use the surface ground, line border, field radius and frontmatter padding, with a (42px) minimum height. Accent color and caret color inherit the blue accent. Settings checkboxes are (20px); library selection checkboxes are (18px). The range input remains native. Errors are red text inside the dialog; source availability and pending operations govern disabled controls.

### Navigation

Three links use muted text at rest, surface ground on hover and raised ground plus primary text for `aria-current="page"`. Desktop links use frontmatter padding; phone links divide the row equally with (8px) padding. Settings remains a quiet labeled action in the header.

### Status and Media Badges

Status labels combine a small dot with text. Healthy, error and recording states use their semantic tokens. Duration/type badges overlay media at the bottom, use tabular numerals and a compact dark fill. They convey source facts, not promotional labels.

### Media Items

Each unboxed media item combines a clipped image button and a compact metadata/action row beneath it. Real images and video thumbnails use `object-fit: cover`; preview media uses `contain`. Missing or damaged previews use the same SVG placeholder and an explicit label. Active recordings disable preview/removal as appropriate; disabled frames preserve full opacity so their status remains readable. Selection adds a checkbox overlay and accent outline without changing grid geometry.

### Dialogs

Native dialogs use the surface ground, dialog radius, overlay shadow and labeled headings. Desktop heading/body/footer horizontal padding is (24px), reducing to (18px) on phones. Thin dividers separate settings sections and footers. Opening moves the dialog upward from (8px) to rest over (180ms) with `cubic-bezier(.16,1,.3,1)`. Reduced motion removes this animation. Native modal behavior provides the focus boundary.

### Notifications and Audio Meter

Notifications sit centered above the bottom edge, using the raised ground, media radius and overlay shadow. They wrap long text and stay within viewport gutters. An undo action remains visible until dismissed or used; notifications without an action expire after (7000ms). The bottom inset changes from (24px) to (14px) on phones.

The audio meter uses a green fill on a slate track; width updates transition over (120ms ease-out). Reduced motion removes that transition. No other decorative animation is part of the shared system.

## Do's and Don'ts

### Do:
- **Do** reuse the shared dark palette, system sans stack and restrained semantic states.
- **Do** preserve visible labels alongside connection and recording state colors.
- **Do** use real media as content, with explicit fallback labels when previews fail.
- **Do** carry the shared focus outline, native dialog behavior and reduced-motion overrides into new components.
- **Do** preserve the two-column phone media grid and reachable navigation/settings pattern.

### Don't:
- **Don't** introduce a marketing display face or a replacement brand identity.
- **Don't** add resting card shadows to the open media grid.
- **Don't** turn synthesized sidecar tonal ramps into additional application tokens.

Not canonized: the finish review notes that small file sizes may round to “0.0 MB.” That is a formatting limitation, not a visual-system rule; this documentation does not change application code. No other material visual defect was identified by the supplied finish review.
