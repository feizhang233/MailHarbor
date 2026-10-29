---
{
  "version": "alpha",
  "name": "MailHarbor",
  "description": "A personal mail control room: compact blue-grey workspace, harbor-green actions, clearly separated account lanes.",
  "colors": {
    "primary": "#11695E", "primary-hover": "#0B5148", "primary-soft": "#E4F1ED",
    "ink": "#172F3C", "muted": "#506773", "background": "#F0F4F6", "surface": "#FFFFFF",
    "line": "#CAD6DD", "line-soft": "#E4EBEF", "rail": "#183B4A", "rail-ink": "#E9F2F5",
    "rail-muted": "#B5CAD3", "danger": "#AB3441", "danger-soft": "#FFF0F1",
    "warning": "#825216", "warning-soft": "#FFF5E3", "focus": "#176BA0",
    "scrollbar-thumb": "#8CA2AC", "scrollbar-track": "#E4EBEF", "scrollbar-hover": "#687F8B",
    "scrollbar-active": "#506773"
  },
  "typography": {
    "body": {"fontFamily": "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang TC', 'Microsoft JhengHei', sans-serif", "fontSize": "0.9375rem", "lineHeight": "1.65"},
    "display": {"fontFamily": "'Avenir Next', 'Segoe UI', 'PingFang TC', sans-serif", "fontSize": "1.75rem", "lineHeight": "1.25"},
    "mono": {"fontFamily": "'SFMono-Regular', Consolas, monospace", "fontSize": "0.8125rem", "lineHeight": "1.6"}
  },
  "rounded": {"control": "0.5rem", "panel": "0.875rem", "badge": "2rem"},
  "spacing": {"xs": "0.375rem", "sm": "0.75rem", "md": "1rem", "lg": "1.5rem", "xl": "2rem", "page-max": "88rem"},
  "components": {"button": {}, "field": {}, "dialog": {}, "status": {}, "mail-row": {}}
}
---

# MailHarbor Design System

## Overview

MailHarbor is a private, single-owner mailbox control room. Its purpose is to let the owner connect many mailboxes, inspect the synchronized messages, and grant read-only access to Codex. The physical reference is a harbor dispatch board: a quiet persistent navigation rail, account labels beside each message, explicit timestamps, and green connection states. The memorable signature is the compact mailbox routing strip above the inbox, showing the actual scope of synchronized mail.

This is a product interface, not a marketing page. The audience uses desktop and phone, with Traditional Chinese interface copy and arbitrary multilingual email content. Europe/Belgrade is the displayed timestamp timezone. No Japan-market business scope is implied. The evidence is CONTRACT.md and the user's multi-mailbox/VPS request. Avoid oversized dashboards, fictional statistics, decorative maritime images, and cards that conceal synchronization failures.

Token ownership is Model A: this frontmatter is the source; `scripts/generate_ui_tokens.py` produces `mailhub/static/tokens.css`. Components consume those custom properties. `--color-*`, `--font-*`, `--text-*`, `--leading-*`, `--radius-*`, and `--space-*` directly map to the corresponding frontmatter paths. Do not edit the generated file. `--check` detects drift.

## Colors

The blue-grey background separates workspace from white reading surfaces. Harbor green means actionable connection or successful availability; red is reserved for errors and destructive confirmation. Warning text is amber-brown on pale amber. Every status also has a text label. The rail is dark teal with pale text, while forms and reading content remain light. This first version has one light theme. Forced-color mode preserves system borders and focus. Scrollbars share the defined global thumb, track, hover, and active tokens.

## Typography

System body fonts avoid external requests and font swaps and cover Traditional Chinese. Avenir Next, when available, is used only for the Latin wordmark and short headings; the fallback retains a practical app character. Body copy uses 15px-equivalent sizing with 1.65 line height; message text uses 16px and 1.8 line height. Technical configuration uses the monospace role. Time and counts use tabular numerals. No body text uses italic.

## Layout

Desktop has a 224px navigation rail and fluid content with a maximum 88rem width. The mail list uses a stable five-row minimum footprint and 30-item server pagination. Each mail row has sender, subject/snippet, mailbox, and time; small screens reflow these into two columns without removing data. At 760px the rail becomes a compact top navigation. Forms and account/token screens scroll in the document; only dialogs and the mail list have bounded local scrolling. Avoid application-wide fixed-height overflow traps.

## Elevation & Depth

Use borders and tonal hierarchy for normal surfaces. Only dialogs and transient status overlays have shadows. A dialog backdrop dims background uniformly and the native modal mechanism makes the background inert. No backdrop blur or card hover elevation.

## Shapes

Controls use the control radius, panels and dialogs use the panel radius, and metadata badges use the badge radius. The wordmark's three short account lines are the only decorative mark. Lists use thin separators rather than individual floating cards.

## Components

Shared `.button` recipes separate solid primary, outline neutral, text/ghost, and danger confirmation. Busy buttons retain labels and dimensions, expose `aria-busy`, and are disabled. Field errors use a persistent reserved line, `aria-invalid`, and `aria-describedby`. Password and token fields are masked with a shared accessible reveal control. Native select popup geometry is explicitly platform-owned. All forms opt out of browser validation bubbles and use shared validation helpers.

Navigation uses real links with `aria-current`; each destination sets a localized document title. Search has its own clear button, a 300ms IME-aware debounce, request cancellation, and memory-only state. Mail rows are semantic buttons, and the reader renders untrusted content only as text. Statuses never claim server synchronization before it completes.

The settings page reuses the same panel, field, confirmation, and copy controls. It keeps synchronization controls together, places each provider's credentials and callback in its own section, and explains masked-secret preservation before Save. The setup checklist uses actual server-returned completion evidence, never decorative progress. AI connection selection uses platform-owned native selects and a single configuration panel; OAuth applications and manual tokens remain distinct lists. OAuth consent displays the requesting application's self-reported name, full callback URI, resource, and complete access scope before the explicit decision.

Dialogs use `<dialog>.showModal()` with labeled titles, scrollable bodies, Escape behavior, and restoration of trigger focus. Destructive actions initially focus Cancel. One shared status region provides polite acknowledgements; persistent errors remain inline. Native dialogs, external assets, hidden message tracking images, and HTML rendering are excluded.

Motion is limited to a 120ms color transition and an indeterminate spinner. Reduced-motion disables animation. Dynamic content reserves empty/loading space. The language is direct: 連接郵箱、同步、斷開連接、建立存取金鑰、撤銷金鑰. Display dates with `zh-TW`, `Europe/Belgrade`, and visible timezone text.

## Do's and Don'ts

- Do show account source, actual cache coverage, and last synchronization time together.
- Do reuse field, dialog, status, and button owners across mailbox and token flows.
- Don't imply that cached recent inbox messages represent the entire mailbox.
- Don't put mail searches, message contents, passwords, or API tokens in persistent browser storage.
