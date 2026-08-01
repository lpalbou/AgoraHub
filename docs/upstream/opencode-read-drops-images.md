# opencode: headless `read` cannot deliver image pixels to a multimodal model

**Package:** opencode (1.18.x) · **Type:** bug/feature gap · **Verified:** 2026-08-02

## The gap

In a headless `opencode run`, a multimodal model (verified with gpt-5.4 via an
OpenAI-compatible provider) has no way to SEE a local image. The `read` tool is
line-oriented text (`[offset=0, limit=20]` — offsets in lines); pointed at a
PNG it returns text-decoded bytes, never an image content-part. There is no
attach/view tool on the headless tool surface.

Reproduced cleanly (permission gates eliminated —