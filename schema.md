# Wiki Schema

This table is the primary routing rule. Add or remove rows to fit your purpose;
keep the source row. Types and folders must be unique lowercase kebab-case names.
Folders are exactly one level below wiki/. The rest of this file is free-form
guidance included in the model prompts.

| Type | Folder | Description |
| --- | --- | --- |
| source | wiki/sources/ | One comprehensive summary per PDF, with evidence and exact structured data. |
| entity | wiki/entities/ | Central named people, organizations, products, systems, or datasets. |
| concept | wiki/concepts/ | Important ideas, techniques, and definitions shared across sources. |
| comparison | wiki/comparisons/ | Evidence-based comparisons with explicit subject and version boundaries. |
| synthesis | wiki/syntheses/ | Conclusions connecting multiple sources, with qualifications and disagreements. |

## Organization

- Create pages because they serve the project purpose, not for every mention.
- Reuse existing pages for the same subject. Do not confuse similar names or
  combine results from different products, versions, populations, or conditions.
- Use portable lowercase ASCII kebab-case filenames. Preserve official names
  and technical identifiers in titles and prose. The source path is supplied.
- Use [[page-slug]] or [[page-slug|label]] cross-references. When slugs collide,
  use [[folder/page-slug|label]]. Link only to existing or planned pages.
- Cite the physical PDF page supporting a claim, including table rows and code.
- Keep exact SQL DDL, schemas, API signatures, configuration, column names,
  types, constraints, keys, indexes, values, units, and caveats intact.
- Page metadata includes type, title, summary, created, updated, tags, related,
  and sources. Python manages dates, source identities, and locked fields.
- Record disagreements with attribution to both sources. Explain differences
  in methods or scope; do not invent a consensus. Flag unresolved conflicts for
  review and revisit a synthesis when new evidence becomes available.
- index.md, overview.md, log.md, and reviews.md are maintained by the pipeline.
