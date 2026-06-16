# Worldpanel AI Reader — Pivot Member Selection Redesign

> Status: Proposed · Date: 2026-06-10 · Supersedes the Data Explorer control model
> in `2026-06-03-worldpanel-ai-reader-design.md` for the pivot/dimension-member path.

## Background

The first version (`2026-06-03`) logs into WorldpanelOnline, opens a Data Explorer
report inside the `#NavigationReportPanel` iframe, parses the rendered Key Measures
table, and answers natural-language questions. It works for the flat KPI / channel /
period dropdowns.

It does **not** satisfy the original product requirement: *"use natural language to
read data from Worldpanel Online."* In the Data Explorer page, after opening the
**pivot screen** (bottom-right), each dimension tag exposes a **`+`** button. The
content revealed inside that `+` cannot be selected by the tool. This document
explains why, and redesigns the product so those members become selectable by
natural language.

## Root cause: why the content inside `+` can't be selected

The current code treats the whole Data Explorer as a set of static `<select>`
dropdowns parsed once from an HTML snapshot. The pivot tag `+` is none of those
things. Three concrete mismatches:

1. **Members are lazy-loaded on click.**
   `data_explorer.py::discover_controls_from_html` runs an `HTMLParser` over a single
   `document.documentElement.outerHTML` snapshot. The members behind a tag's `+`
   (Hyper / CVS, manufacturers, pack types, nested product hierarchy, …) are rendered
   into the DOM via AJAX/postback **only after** the `+` is clicked. The snapshot is
   taken before that click, so those members were never in the parser's view.

2. **It is a nested checkbox tree, not a flat list.**
   The data model `DataExplorerDimension.options` is a one-dimensional
   `(label, value)` tuple list, and `client.py::_select_dimension` uses
   `select.select_option(value=...)`. The `+` reveals a Telerik `RadTreeView`:
   multi-level, expandable, checkbox-driven. A one-dimensional model cannot express
   "parent → child member → grandchild," and `select_option` does not apply to it.

3. **The selection action is too fragile.**
   `client.py::_select_segment` clicks a pivot button, then does
   `frame.locator("text={segment}").click()`. In the `+` scenario this necessarily
   fails: the member isn't rendered yet (not found); duplicate labels at different
   tree levels hit the wrong node; virtualized/collapsed nodes can't be clicked. Worse,
   `segment` is scraped from the **`+` button's own label**, not from the real members
   inside it.

In short: the tool can *see* the `+` button, but it never actually clicks it open,
enumerates the members behind it, or selects them the correct way (checking tree
nodes). That is exactly the reported failure.

There is also a product-level limitation: the NL layer
(`query.py::_find_product`, `data_explorer.py::_find_dimensions`) is **regex plus a
hard-coded dictionary** (Coke TM, Hyper, 金果, …). Even if the members were
discovered, a fixed vocabulary cannot map arbitrary natural language onto the
hundreds of dynamic members that live inside those trees. **Selecting `+` content by
natural language is structurally impossible with a fixed dictionary.**

## Redesign in one sentence

Shift from *"take one HTML snapshot and guess dimensions with regex"* to *"drive the
real UI live, and let an LLM map intent against the live schema."*

Two paradigm shifts:

| Aspect            | Current                              | Redesigned                                                            |
| ----------------- | ------------------------------------ | -------------------------------------------------------------------- |
| Control discovery | Parse one `outerHTML` snapshot       | Drive the DOM live: click `+`, wait for members, read the tree       |
| Dimension model   | One-dimensional `options` list       | Hierarchical member tree `MemberNode` (lazy children)                |
| Selection action  | `select_option` / `text=` click      | Dispatch by control type: `<select>` → select; tree → check nodes    |
| NL mapping        | Hard-coded regex dictionary          | Discover live members first, then LLM maps intent to real members    |
| Waiting           | `asyncio.sleep(2)` everywhere        | Event-driven waits (`wait_for_selector` / await XHR response)        |
| Pivot layout      | Read-only hints about Row / Column   | Read, set, reorder, and verify Row / Column dimensions               |
| Browser lifecycle | New browser/page for each operation  | One persistent report session per active user query                  |
| Result confidence | Assume a refreshed table is correct  | Verify applied layout and members before accepting the result        |

## Target architecture

Keep the FastAPI + local browser UI shell. Split the internals into four clear layers.

```
Natural-language question
   |
+--v-------------+   (1) intent + plan
|  Planner (LLM) |   question -> action sequence; ambiguity -> clarify
+--+-------------+
   |  needs to know which dimensions/members exist
+--v-------------+   (2) live schema
| SchemaService  |   discover dimensions; expand + on demand; cache (TTL)
+--+-------------+
   |  invokes low-level actions
+--v-------------+   (3) browser driver (core new component)
|  PivotDriver   |   open_pivot / expand_member(+) / list_members
|  (Playwright)  |   / search / check / apply / read_result
+--+-------------+
   |
+--v-------------+   (4) result parsing
| ResultParser   |   render -> structured rows (reuse existing parser)
+----------------+
```

## Query contract and execution transaction

The redesigned path must treat one natural-language question as a single, stateful
transaction against one live Data Explorer page. Opening a fresh browser for every
step loses lazy-loaded tree state and makes multi-step pivot changes unreliable.

The planner produces an explicit, model-independent `QueryPlan`:

```python
@dataclass(frozen=True)
class AxisPlacement:
    dimension: str
    axis: Literal["row", "column", "filter", "available"]
    position: int

@dataclass(frozen=True)
class MemberSelection:
    dimension: str
    member_path: tuple[str, ...]
    checked: bool = True

@dataclass(frozen=True)
class QueryPlan:
    report_set: str
    report: str
    axis_placements: tuple[AxisPlacement, ...]
    member_selections: tuple[MemberSelection, ...]
    kpis: tuple[str, ...]
    expected_period: str | None
    output_shape: Literal["single_value", "table", "comparison", "trend"]
```

Execution happens in this order:

```
acquire persistent report session
-> inspect current pivot layout
-> set/reorder Row and Column dimensions
-> expand only required dimension trees
-> check/uncheck required members
-> apply
-> verify the applied state
-> read and parse the refreshed table
-> attach an execution receipt to the answer
```

Each active user session owns one serialized `DataExplorerSession` containing the
browser context, page/frame handles, current report, current pivot state, and cache
references. Queries for the same session run under a lock so concurrent requests
cannot change each other's filters. On failure, the session either restores the last
verified state or is discarded and recreated cleanly.

## Core new component: `PivotDriver`

This is the heart of the redesign. It no longer parses a snapshot; it operates the
pivot screen the way a person does.

```python
class PivotDriver:
    async def open_pivot(self) -> None: ...
    # tags in the pivot (Row area / Column area / available area)
    async def list_dimension_tags(self) -> list[DimensionTag]: ...

    # --- Row / Column layout operations required for cross-tab questions ---
    async def read_layout(self) -> PivotLayout: ...
    async def set_axis(self, tag: DimensionTag, axis: str, position: int) -> None:
        """Move or configure a dimension in Row / Column / filter / available."""
    async def remove_dimension(self, tag: DimensionTag) -> None: ...
    async def verify_layout(self, expected: PivotLayout) -> None: ...

    # --- the three methods that solve "+" ---
    async def expand_member(self, tag: DimensionTag, path: list[str]) -> None:
        """Click a tag's + (or a tree node at some level); wait for that node's
        children to actually render. Use event waits, not sleep:
        await an XHR response / wait for the child-node selector to appear."""

    async def list_members(self, tag, path) -> list[MemberNode]:
        """Read members at the currently expanded level (with checked state and
        whether the node has further children)."""

    async def search_member(self, tag, text) -> list[MemberNode]:
        """Prefer the tree's own search box when present, to avoid virtualized
        scrolling making nodes unreachable."""

    async def check_member(self, tag, member, checked=True) -> None:
        """Check / uncheck a tree node's checkbox — the correct way to select a
        member inside +."""

    async def apply(self) -> None:
        """Click Apply/OK so the pivot recomputes; wait for the table refresh."""

    async def read_applied_state(self) -> AppliedPivotState:
        """Read the actual layout and checked members after Apply."""
```

It completes the broken chain into:

```
open_pivot -> set/reorder Row and Column tags -> find target tag
-> expand_member (click +) -> wait for members
-> list_members / search_member -> check_member (check the box) -> apply
-> verify applied state -> read result
```

Implementation notes, mapped directly to the three failure causes:

- **Lazy load** — after `expand_member`, wait with `frame.expect_response(...)` or
  `wait_for_selector(child node)` instead of `sleep(2)`. On timeout, retry or raise a
  clear error.
- **Nested tree** — the new `MemberNode` is a tree
  (`label / value / level / has_children / checked / parent_path`) supporting
  recursive expansion by path, so multi-level product/channel hierarchies are
  locatable.
- **Selection method** — click the node **checkbox** (located via the Telerik
  `RadTreeView` DOM structure or role/accessible-name), not a whole-row `text=`
  click; disambiguate same-named nodes by `parent_path`.
- **Virtualized / long lists** — prefer the tree's own search box; otherwise scroll
  to load until the target is hit.
- **Cross-tab layout** — manipulate dimensions through the pivot screen's supported
  move/configure controls, then verify the visible Row / Column order. Member
  selection alone is not enough for questions that require a different table shape.
- **State verification** — after Apply, compare actual Row / Column tags and checked
  member paths to the `QueryPlan`. A refreshed table is rejected if the applied state
  does not match.

> **Optional, more robust (recommended for V2):** Worldpanel is Telerik ASP.NET, so
> "expand node / apply pivot" is backed by stable AJAX endpoints. Capture those XHRs
> in the browser once, then **replay the requests directly** to fetch members and data,
> bypassing the fragility of clicking entirely. Ship V1 on DOM driving first; optimize
> with XHR replay in V2.

## NL layer: map intent against live members with an LLM

Replace the hard-coded regex dictionaries in `query.py` / `data_explorer.py` with a
model-independent structured planner that continues to use the application's
configured shared AI API. The planner must not depend on one model vendor.

Do **not** enumerate every member from every tree before each question. Product and
classification trees may contain hundreds or thousands of nodes, making full
discovery slow and expensive. Use progressive, on-demand discovery:

1. **Parse dimension intent first.** The LLM extracts tentative dimensions, values,
   period, KPI, desired Row / Column layout, and output shape without claiming that
   member names are valid.
2. **Open only relevant dimensions.** `SchemaService` expands only the trees needed
   by the tentative plan.
3. **Search before traversal.** Prefer the dimension tree's own search UI. Feed only
   matching candidates and their parent paths to the LLM.
4. **Resolve or clarify.** Select an exact member only when the candidate is unique
   enough. When labels are missing or ambiguous, return a clarification with real
   candidate choices.
5. **Compile the final `QueryPlan`.** Every selected value must reference a
   live-discovered label and full member path.

For example, "金果 2024 P6 Spend in Hyper channel, Product on rows and Period on
columns" becomes:

```json
{
  "axis_placements": [
    {"dimension": "Product", "axis": "row", "position": 0},
    {"dimension": "Period", "axis": "column", "position": 0}
  ],
  "member_selections": [
    {"dimension": "Product", "member_path": ["Kiwifruit", "Gold Kiwifruit"]},
    {"dimension": "Channel", "member_path": ["TTL Channel", "Hyper"]}
  ],
  "kpis": ["Spend (RMB 000)"],
  "expected_period": "2024 P6",
  "output_shape": "single_value"
}
```

Because the mapping target is live-discovered real members rather than a fixed
dictionary, members inside `+` become selectable by natural language regardless of
how many manufacturers, brands, packs, or nested SKUs exist.

## SchemaService cache policy

Schema discovery and result data have different lifetimes and must use separate cache
keys:

- **Dimension schema cache:** report set + report + dimension tag; stores member
  paths, child availability, and discovery timestamp with a short TTL.
- **Search candidate cache:** report + dimension + normalized search text; stores the
  candidate paths returned by the tree search.
- **Result cache:** report + verified Row / Column layout + checked member paths + KPI
  + period; stores parsed table data.

Cache entries are used only as hints. Before returning a cached result, the system
shows the exact saved filter receipt. When Worldpanel changes a member tree or a
selection cannot be applied, invalidate the relevant schema entry and rediscover it.

## Applied-state verification and answer receipt

After `apply()`, the driver must read the actual UI state and compare it to the plan
before parsing the table. The answer response includes a receipt so users and data
checkers can see exactly which Worldpanel selections produced the number:

```json
{
  "row_dimensions": ["Product"],
  "column_dimensions": ["Period", "KPI"],
  "selected_members": {
    "Product": [["Kiwifruit", "Gold Kiwifruit"]],
    "Channel": [["TTL Channel", "Hyper"]]
  },
  "kpis": ["Spend (RMB 000)"],
  "period": "2024 P6",
  "table_refreshed": true,
  "verified": true
}
```

If layout, member checks, KPI, or period do not match, do not answer from the table.
Return a clear execution error or ask the user to clarify the intended selection.

## Reliability fixes to make along the way

- Replace all `asyncio.sleep(2)` polling with event-driven waits
  (`wait_for_load_state`, `expect_response`, `wait_for_selector`).
- Stabilize selectors: rely on Telerik control structure + role/accessible-name
  rather than brittle text matching.
- iframe nesting: explicitly handle any nested frames inside `#NavigationReportPanel`.
- Keep "captured raw DOM fragment" in errors for diagnosis, but never leak the password.
- Treat Phase 0 findings as a delivery gate. The Telerik `RadTreeView`, search,
  checkbox, axis-move, and Apply mechanisms are hypotheses until confirmed on the
  real authorized page.

### Phase 0 probe findings (2026-06-10)

The authorized-page probe confirmed the path and corrected several assumptions:

- Data Explorer first opens a Navigator page. The report controls appear only after
  `#NavigationReportPanel` loads the Key Measures report.
- Pivot Screen opens `dialogs/DB_Pivoting.aspx` in an additional frame. Its
  Row/Column/available-dimension controls are Telerik `RadListBox` controls.
- Clicking a dimension's magnifying-glass image opens
  `dialogs/SimpleSelector.aspx` in another frame. The member control is a Telerik
  `RadTreeView`.
- Expanded hierarchy nodes use `.rtMinus`; collapsed hierarchy nodes use `.rtPlus`.
  Some dimensions, such as Period, contain only leaf nodes and therefore expose no
  `+`. The probe targets a hierarchical dimension such as Product.
- On the observed Product tree, children were already rendered in the DOM. Clicking
  one real `.rtPlus` changed it to `.rtMinus` without changing node count and without
  triggering XHR/fetch traffic. Therefore child-node appearance and awaited XHR are
  not valid universal expansion checks; verify the expand-control state instead.
- The XHR-replay route is not justified for this observed expand action. Keep it as
  an optional route only for actions that Phase 0 later proves are server-backed.

Probe command:

```powershell
python scripts\probe-pivot-member-tree.py
```

Redacted evidence is written under ignored `runtime/phase0-probe/`. Search, member
checking, and Apply remain separate Phase 0 gates before their drivers are built.

## Security and data handling

- Continue reading credentials and the shared AI API key from local environment
  configuration. Never place them in code, browser diagnostics, fixtures, or Git.
- Redact cookies, authorization headers, ASP.NET state tokens, email addresses, and
  business values from captured DOM/XHR fixtures before committing them.
- Send the LLM only the minimum candidate labels and paths needed to resolve the
  current question. Do not send complete report tables or full member trees unless a
  user explicitly requests an AI analysis that requires them.
- Keep the planner provider-agnostic and compatible with the existing configured
  chat-completions endpoint.
- Log the plan, action status, and redacted receipt; do not log credentials or raw
  authenticated network payloads.

## Three technical routes (decision point)

| Route                                                                       | Solves `+`         | Stability               | Effort                 | Recommendation   |
| --------------------------------------------------------------------------- | ------------------ | ----------------------- | ---------------------- | ---------------- |
| **A. DOM driving** (`PivotDriver`, the main line of this design)            | Yes — real click + real check | Medium (DOM-structure dependent) | Medium    | **Do this first** |
| **B. XHR replay** (call Telerik endpoints directly)                         | Yes — most thorough | High                   | High (reverse-engineer) | V2 optimization  |
| **C. Agentic browser** (LLM drives by DOM/screenshot, e.g. computer-use)    | Yes — no fixed selectors | Medium (occasional misclicks) | Low             | Prototype / fallback |

Recommendation: run the **main line on A** with the NL layer swapped to an LLM. Once A
works, use B for speed and stability; keep C as a quick prototype or a fallback for
hard pages.

## Phased delivery

- **Phase 0 — gated real-page probe.** On the authorized real page, record the
  redacted DOM and network behavior for opening Pivot Screen, moving Row / Column
  tags, clicking one tag's `+`, expanding a child, searching, checking a member, and
  applying. Confirm or correct the Telerik assumptions before implementation.
- **Phase 1 — persistent `DataExplorerSession`.** Keep one report page alive for a
  serialized query transaction; add reset/recovery behavior and session expiry.
- **Phase 2 — Pivot layout driver.** Implement read/set/reorder/remove/verify for Row
  and Column dimensions with fixture-based tests.
- **Phase 3 — Pivot member driver.** Implement expand/list/search/check/apply and
  applied-state verification with real redacted DOM fixtures.
- **Phase 4 — on-demand `SchemaService`.** Add dimension/member search and separate
  TTL caches without performing full-tree enumeration by default.
- **Phase 5 — LLM Planner.** Replace fixed dictionaries with provider-agnostic
  structured planning, real-candidate matching, and the clarification loop.
- **Phase 6 — reliability and observability.** Remove sleeps, stabilize selectors,
  add redacted diagnostics, session recovery, and action receipts.
- **Phase 7 — optional XHR replay.** Introduce only after the captured requests and
  permission constraints are understood.

## Test and acceptance plan

### Unit and fixture tests

- Parse Row / Column / available dimension tags from captured pivot fixtures.
- Represent duplicate member labels under different parent paths without collision.
- Expand a lazy node, search a long/virtualized tree, check/uncheck a member, and
  verify applied state.
- Compile structured LLM output only when every member maps to a discovered path.
- Return clarification for missing dimensions, ambiguous paths, and unavailable
  members.
- Verify cache keys differ when Row / Column order, member path, KPI, or period differs.
- Redact secrets and authenticated state from diagnostic fixtures.

### Authorized end-to-end acceptance scenarios

1. Ask: "Coke TM 2025 全年 Hyper 渠道的销额是多少？"
   - Product and Channel members are selected from their `+` trees.
   - The receipt shows the exact member paths, period, and KPI.
2. Ask: "把 Product 放在 Row，Period 和 KPI 放在 Column，查看 Coke TM 和 Coke Zero
   2025 全年的销额和渗透率。"
   - The driver changes and verifies the Row / Column layout before reading the table.
3. Select a member whose label exists under multiple parents.
   - The system asks for clarification or uses the requested parent path; it never
     silently picks the first text match.
4. Ask for an unavailable channel or product.
   - The UI offers real candidates discovered from Worldpanel.
5. Repeat an identical verified query.
   - The result cache is used and the saved filter receipt is displayed.
6. Ask a second, different question in the same session.
   - Previous selections do not leak into the new result unless explicitly retained.
7. Force a failed Apply or stale tree.
   - The system does not answer from the stale table; it recovers or returns a clear
     failure.

The redesign is accepted only when scenarios 1-7 pass against an authorized live
report and the returned values are manually spot-checked against the same visible
Worldpanel table.

## Definition of done

This redesign satisfies the original product requirement only when a user can ask a
natural-language question that requires:

- selecting KPI, product, period, category, channel, and classification values,
- opening the relevant Pivot Screen `+` trees and selecting nested members,
- changing Row / Column dimensions when the requested output requires it,
- receiving clarification instead of an invented/default selection,
- and receiving a value or table with a verified, visible filter receipt.

A document, successful tree click, or refreshed table alone is not sufficient.

## Out of scope / unchanged

- Login, Report Set selection, Ready-to-Use catalog navigation, and result-table
  parsing stay as they are.
- The tool still does not bypass site permissions, captcha, two-factor login, or
  access controls, and does not connect to the Worldpanel database directly.
