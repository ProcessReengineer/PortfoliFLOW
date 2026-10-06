# Module Specification: [module_name]

## Area
<!-- One of the values in VALID_AREAS (core/base_module.py). A new Area needs an ADR. -->

## Purpose
<!-- One paragraph: what does this module do and why does it exist? -->

## Inputs
<!-- Bullet list of data this module consumes. Include types. -->

## Outputs
<!-- Bullet list of data this module produces. Include types. -->

## Dependencies (internal)
<!-- Which core/ or services/ modules does this module import from? -->

## Dependencies (external)
<!-- Which third-party Python packages does this module require? -->

## Public API
<!-- List method signatures with brief descriptions. -->

## Persistence
<!-- Which tables does this module read or write, through which core/repositories/ classes? -->
<!-- New modules do not use the in-memory DataStore; it is legacy (roadmap #035). -->
<!-- Write "None" if the module persists nothing. -->

## Web view
<!-- The route in web/routes/, the Section (SectionMeta in web/shell.py) and the partials it adds. -->
<!-- See AGENTS.md, "Adding a module or a view", and docs/ux/ui-standards.md §7. -->

## Test plan
<!-- Route tests in tests/web/, service tests in tests/services/, repository tests in -->
<!-- tests/repositories/ (AGENTS.md, "Adding a module or a view", step 6). -->
