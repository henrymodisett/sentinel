# UI Design Lens

## When to apply
Projects with a user-facing frontend (web, mobile, desktop).

## What to look for
- Consistency: do similar components look and behave the same way?
- Hierarchy: is information priority clear through visual weight?
- Responsiveness: does the layout work across screen sizes?
- Feedback: does every user action get a visible response?
- Typography: is text readable, well-spaced, with clear hierarchy?

## Smells
- Inconsistent spacing, colors, or component styles across pages
- Buttons or interactive elements with no hover/active/focus states
- Forms with no validation feedback until submit
- Loading states that show nothing (blank screen, no skeleton/spinner)
- Text that's too small, too dense, or low-contrast
- Modals on top of modals
- Important actions buried in menus or requiring too many clicks
- No dark mode consideration when the platform supports it

## What good looks like
- Design system with reusable components — not one-off styles
- Clear visual hierarchy — you know what's important at a glance
- Every state is designed: empty, loading, error, success, partial
- Consistent spacing scale (4px/8px grid)
- Animations are purposeful (guide attention, confirm actions) not decorative
- Accessible: keyboard navigable, screen reader compatible, sufficient contrast

## Questions to ask
- Can a new user complete the primary task without instructions?
- What does the UI look like with no data? With an error? While loading?
- Is there a component library or is every page hand-crafted?
- How does it look on mobile? On a 4K display?
