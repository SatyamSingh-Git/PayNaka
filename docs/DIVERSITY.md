### Corpus diversity

- **342** attack cases, **342** distinct payloads
- mean payload length **200** characters
- pairwise lexical similarity: mean **0.110**, p95 **0.357**, max **0.951**
- pairs above 0.90 cosine: **106** (106 same-seed, 0 cross-seed)
- exact duplicate payloads: **0**

Same-seed near-duplicates are expected and are not a defect: one seed is
deliberately re-framed several ways, and two framings of one payload should
look alike. **Cross-seed** near-duplicates are the number that matters -- each
one would mean two nominally distinct attacks are really the same attack, and
the corpus is smaller than it claims.

| Family | Cases |
| --- | ---: |
| currency_confusion | 36 |
| destination_swap | 36 |
| line_item_append | 72 |
| obfuscated_payload | 48 |
| quantity_inflation | 42 |
| refund_without_return | 36 |
| replay_double_charge | 30 |
| tool_call_smuggling | 42 |

| Vector | Cases |
| --- | ---: |
| description | 54 |
| image_alt | 36 |
| review | 180 |
| seller_note | 72 |
