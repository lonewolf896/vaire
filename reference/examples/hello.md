# Example Reference

This is a placeholder reference document. Replace the `reference/` directory contents
with your own reference material (standards, frameworks, operational docs).

## Intro

Reference files are static content served by `load_reference()`. They are:
- Stored in `reference/` and baked into the Docker image at `/app/reference/`
- Indexed via `manifest.json` with topic keys, categories, and section indexes
- Hash-verified on load (for categories with `"integrity": "required"`)

## Usage

```python
# List all available references
load_reference(show_index=True)

# Load a specific reference
load_reference(topic="example:hello")

# Load a specific section
load_reference(topic="example:hello", section="usage")
```

See `CLAUDE.md` for the full reference system documentation.
