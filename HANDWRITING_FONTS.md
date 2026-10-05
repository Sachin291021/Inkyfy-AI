# Inkyfy AI handwriting font additions

Added/confirmed realistic handwriting styles:

- Caveat — Natural everyday handwriting
- Caveat Brush — Rougher pen-written style
- Shadows Into Light — Personal notebook style
- Covered By Your Grace — Casual handwritten notes
- Nanum Pen Script — Compact pen script

Shadows Into Light, Covered By Your Grace, and Nanum Pen Script were already bundled in the project.
Caveat and Caveat Brush are downloaded automatically on first use if they are not bundled, and are also included in `static/fonts/download_fonts.py` for offline/local preparation.
\n## Reliability fix\n\nCaveat and Caveat Brush are now fully registered in the renderer. The server attempts to download the real fonts when they are missing; if a local/offline package does not contain the font files, conversion falls back to bundled handwriting fonts instead of failing with a missing-font error.\n