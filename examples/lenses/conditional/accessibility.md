# Accessibility Lens

## When to apply
Projects with a user-facing UI (web, mobile, desktop).

## What to look for
- Keyboard navigation: can every interactive element be reached and activated without a mouse?
- Screen readers: do images have alt text? Do form fields have labels? Is content structured with headings?
- Color: is information conveyed by more than just color? Is contrast sufficient?
- Motion: can animations be disabled? Do they respect prefers-reduced-motion?
- Focus management: is focus visible? Does it move logically through the page?

## Smells
- Click handlers on divs/spans instead of buttons/links
- Images without alt attributes (or alt="" on informational images)
- Form inputs without associated labels
- Color as the only indicator of state (red=error, green=success, with no icon or text)
- Contrast ratio below 4.5:1 for body text
- Custom components that don't expose ARIA roles
- Modals or overlays that trap focus incorrectly (or don't trap it at all)
- Auto-playing video or audio with no way to pause

## What good looks like
- Semantic HTML: buttons are buttons, links are links, headings form a logical outline
- WCAG 2.1 AA compliance as a baseline
- Tab order matches visual order
- Error messages are associated with their fields (aria-describedby)
- Skip-to-content link for keyboard users
- Automated accessibility testing in CI (axe-core, Lighthouse)

## Questions to ask
- Can you complete the primary user flow using only a keyboard?
- What does a screen reader announce on the landing page?
- Are there any images that convey information but have no alt text?
- Has anyone tested with an actual screen reader (VoiceOver, NVDA)?
