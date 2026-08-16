/**
 * SAA section client — picker, configuration editor, asset-class modal.
 *
 * Embedded in /back-office#saa. Loaded by saa_section.html via a
 * deferred <script src> tag. Re-initialises on every htmx:afterSwap
 * that lands inside #pf-saa-root (picker switch, new-config swap,
 * delete-reload). The Tabulator instances for the inputs table,
 * correlation matrix, and asset-class modal table are kept in
 * module-scoped variables that are rebuilt on each re-init.
 *
 * Surfaces:
 *   - GET    /api/saa/section?config_id=<uuid>     (full-section refresh)
 *   - GET    /api/saa/configuration/<id>           (picker switch)
 *   - PUT    /api/saa/configuration/<id>           (save)
 *   - POST   /api/saa/configuration                (create)
 *   - POST   /api/saa/configuration/<id>/activate  (activate)
 *   - DELETE /api/saa/configuration/<id>           (delete)
 *   - GET    /api/saa/asset-classes                (modal content)
 *   - POST   /api/saa/asset-classes                (create)
 *   - PUT    /api/saa/asset-classes/<id>           (update)
 *   - DELETE /api/saa/asset-classes/<id>           (delete)
 */
(function () {
    "use strict";

    // -----------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------

    function getCSRFToken() {
        const meta = document.querySelector("meta[name='csrf-token']");
        return meta ? meta.getAttribute("content") : "";
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    function openDialog(dlg) {
        if (!dlg) return;
        if (typeof dlg.showModal === "function") dlg.showModal();
        else dlg.setAttribute("open", "");
    }

    function closeDialog(dlg) {
        if (!dlg) return;
        if (typeof dlg.close === "function") dlg.close();
        else dlg.removeAttribute("open");
    }

    async function fetchJson(url, options) {
        const opts = options || {};
        opts.headers = Object.assign(
            {
                "X-CSRF-Token": getCSRFToken(),
            },
            opts.headers || {}
        );
        const response = await fetch(url, opts);
        let body = null;
        try { body = await response.json(); } catch (e) { body = null; }
        return { response, body };
    }

    // -----------------------------------------------------------------
    // Section-level state
    // -----------------------------------------------------------------

    let inputsTable = null;
    let matrixTable = null;
    let assetClassesTable = null;

    function readBootstrap() {
        const tag = document.getElementById("saa-config-bootstrap");
        if (!tag) return null;
        try { return JSON.parse(tag.textContent); }
        catch (e) { return null; }
    }

    // -----------------------------------------------------------------
    // Section reload
    // -----------------------------------------------------------------

    function reloadSection(configIdToPin) {
        const root = document.getElementById("pf-saa-root");
        if (!root || typeof window.htmx === "undefined") {
            // Fallback: full reload of the area page.
            const fragment = configIdToPin
                ? "#saa-config-" + configIdToPin
                : "#saa";
            window.location.href = "/back-office" + fragment;
            return;
        }
        const url = configIdToPin
            ? "/api/saa/section?config_id=" + encodeURIComponent(configIdToPin)
            : "/api/saa/section";
        window.htmx.ajax("GET", url, {
            target: "#pf-saa-root",
            swap: "outerHTML",
        });
    }

    // -----------------------------------------------------------------
    // URL-fragment deep-linking
    // -----------------------------------------------------------------

    function maybeFollowFragment() {
        const hash = window.location.hash || "";
        const match = hash.match(/^#saa-config-([0-9a-fA-F-]{36})$/);
        if (!match) return;
        const bootstrap = readBootstrap();
        if (bootstrap && bootstrap.config_id === match[1]) return;
        reloadSection(match[1]);
    }

    // -----------------------------------------------------------------
    // Picker
    // -----------------------------------------------------------------

    function wirePicker(root) {
        const switcher = root.querySelector("#saa-config-switcher");
        if (switcher) {
            switcher.addEventListener("change", function () {
                const id = switcher.value;
                if (!id) return;
                if (typeof window.htmx !== "undefined") {
                    window.htmx.ajax(
                        "GET",
                        "/api/saa/configuration/" + encodeURIComponent(id),
                        { target: "#saa-config-body", swap: "innerHTML" }
                    );
                }
            });
        }

        root.querySelectorAll("[data-action='activate']").forEach(function (btn) {
            btn.addEventListener("click", function () {
                activateConfiguration(btn.getAttribute("data-config-id"));
            });
        });
        root.querySelectorAll("[data-action='delete']").forEach(function (btn) {
            btn.addEventListener("click", function () {
                deleteConfiguration(btn.getAttribute("data-config-id"));
            });
        });

        const newBtn = root.querySelector("#saa-new-config-btn");
        if (newBtn) {
            newBtn.addEventListener("click", function () {
                openDialog(document.getElementById("saa-new-config-dialog"));
            });
        }
        const newBtnEmpty = root.querySelector("#saa-new-config-btn-empty");
        if (newBtnEmpty) {
            newBtnEmpty.addEventListener("click", function () {
                openDialog(document.getElementById("saa-new-config-dialog"));
            });
        }

        const manageBtn = root.querySelector("#saa-manage-asset-classes-btn");
        if (manageBtn) {
            manageBtn.addEventListener("click", openAssetClassesModal);
        }
    }

    // -----------------------------------------------------------------
    // Configuration lifecycle
    // -----------------------------------------------------------------

    async function activateConfiguration(configId) {
        if (!configId) return;
        if (!confirm("Make this configuration the tenant's active SAA?")) return;
        const { response } = await fetchJson(
            "/api/saa/configuration/" + encodeURIComponent(configId) + "/activate",
            { method: "POST" }
        );
        if (response.ok) reloadSection(configId);
        else alert("Activation failed (" + response.status + ").");
    }

    async function deleteConfiguration(configId) {
        if (!configId) return;
        if (!confirm("Delete this configuration? This cannot be undone.")) return;
        const { response, body } = await fetchJson(
            "/api/saa/configuration/" + encodeURIComponent(configId),
            { method: "DELETE" }
        );
        if (response.ok) reloadSection(null);
        else alert("Deletion failed: " + ((body && body.detail) || response.status));
    }

    function wireNewConfigDialog() {
        const dlg = document.getElementById("saa-new-config-dialog");
        const form = document.getElementById("saa-new-config-form");
        if (!dlg || !form) return;

        dlg.querySelectorAll("[data-action='cancel-new-config']").forEach(function (btn) {
            btn.addEventListener("click", function () { closeDialog(dlg); });
        });

        form.addEventListener("submit", async function (evt) {
            evt.preventDefault();
            const errEl = document.getElementById("saa-new-config-error");
            if (errEl) { errEl.hidden = true; errEl.textContent = ""; }
            const formData = new FormData(form);
            const { response, body } = await fetchJson(
                "/api/saa/configuration",
                { method: "POST", body: formData }
            );
            if (response.ok && body && body.id) {
                closeDialog(dlg);
                form.reset();
                reloadSection(body.id);
                return;
            }
            if (errEl) {
                errEl.hidden = false;
                errEl.textContent = (body && body.error)
                    || ("Create failed (" + response.status + ").");
            }
        });
    }

    // -----------------------------------------------------------------
    // Inputs table + Correlation matrix
    // -----------------------------------------------------------------

    function orderedKey(a, b) { return a < b ? a + "|" + b : b + "|" + a; }

    function nameForAssetClass(lookup, id) {
        const ac = lookup[id];
        return ac ? ac.display_name : "—";
    }

    function preserveCorrelationsAsMap(prevTable) {
        const map = {};
        if (!prevTable) return map;
        const columns = prevTable.getColumns();
        const idCols = columns.filter(function (c) {
            return c.getDefinition().userData
                && c.getDefinition().userData.assetClassId;
        });
        const colIdsByField = {};
        idCols.forEach(function (col) {
            colIdsByField[col.getField()] =
                col.getDefinition().userData.assetClassId;
        });
        prevTable.getData().forEach(function (row) {
            const rowId = row._asset_class_id;
            if (!rowId) return;
            Object.keys(colIdsByField).forEach(function (field) {
                const colId = colIdsByField[field];
                if (rowId === colId) return;
                const value = row[field];
                if (value === null || value === undefined || value === "") return;
                const num = Number(value);
                if (Number.isNaN(num)) return;
                map[orderedKey(rowId, colId)] = num;
            });
        });
        return map;
    }

    function buildCorrelationMatrix(state, prevPreservedMap, idsOverride) {
        const lookup = state.assetClassLookup;
        // For the initial render we pass idsOverride because
        // inputsTable.getData() is unreliable immediately after Tabulator
        // construction. For subsequent rebuilds (row add/delete/edit),
        // getData() is the right source — the table is fully built.
        const ids = idsOverride !== undefined
            ? idsOverride.slice()
            : state.inputsTable.getData()
                .map(function (r) { return r.asset_class_id; })
                .filter(function (id) { return !!id; });

        ids.sort(function (a, b) {
            return nameForAssetClass(lookup, a)
                .localeCompare(nameForAssetClass(lookup, b));
        });

        const preservedMap = prevPreservedMap || {};
        if (!prevPreservedMap) {
            (state.initialCorrelations || []).forEach(function (c) {
                preservedMap[orderedKey(c.asset_class_a_id, c.asset_class_b_id)]
                    = c.correlation;
            });
        }

        const data = ids.map(function (rowId) {
            const row = {
                _asset_class_id: rowId,
                _row_label: nameForAssetClass(lookup, rowId),
            };
            ids.forEach(function (colId, colIdx) {
                const field = "col_" + colIdx;
                if (rowId === colId) {
                    row[field] = 1;
                } else {
                    const v = preservedMap[orderedKey(rowId, colId)];
                    row[field] = (v === undefined || v === null) ? null : v;
                }
            });
            return row;
        });

        const columns = [
            {
                title: "",
                field: "_row_label",
                frozen: true,
                width: 200,
                headerSort: false,
                formatter: function (cell) {
                    return "<strong>" + escapeHtml(cell.getValue()) + "</strong>";
                },
            },
        ];
        ids.forEach(function (id, idx) {
            columns.push({
                title: nameForAssetClass(lookup, id),
                field: "col_" + idx,
                hozAlign: "center",
                headerSort: false,
                width: 110,
                userData: { assetClassId: id, columnIndex: idx },
                editor: "number",
                editorParams: { min: -1, max: 1, step: 0.01 },
                editable: function (cell) {
                    const rowIdx = cell.getRow().getPosition() - 1;
                    return idx > rowIdx;
                },
                validator: ["min:-1", "max:1"],
                cssClass: "saa-correlation-cell",
                formatter: function (cell) {
                    const rowIdx = cell.getRow().getPosition() - 1;
                    const colIdx = idx;
                    const v = cell.getValue();
                    if (rowIdx === colIdx) {
                        cell.getElement().classList.add("saa-cell-diagonal");
                        return '<span class="saa-cell-diagonal-text">1.00</span>';
                    }
                    if (colIdx < rowIdx) {
                        cell.getElement().classList.add("saa-cell-mirror");
                        const mirrorRow = matrixTable
                            ? matrixTable.getRowFromPosition(colIdx + 1)
                            : null;
                        if (mirrorRow) {
                            const mv = mirrorRow.getCell("col_" + rowIdx).getValue();
                            return mv === null || mv === undefined
                                ? "—" : Number(mv).toFixed(2);
                        }
                        return v === null || v === undefined
                            ? "—" : Number(v).toFixed(2);
                    }
                    cell.getElement().classList.add("saa-cell-upper");
                    return v === null || v === undefined
                        ? "—" : Number(v).toFixed(2);
                },
            });
        });

        const previous = matrixTable;
        if (previous) previous.destroy();
        matrixTable = new Tabulator("#saa-correlation-matrix-table", {
            data: data,
            layout: "fitDataStretch",
            columns: columns,
        });
        matrixTable.on("cellEdited", function (cell) {
            const colDef = cell.getColumn().getDefinition();
            const colIdx = colDef.userData ? colDef.userData.columnIndex : -1;
            const rowIdx = cell.getRow().getPosition() - 1;
            if (colIdx > rowIdx) {
                const mirrorRow = matrixTable.getRowFromPosition(colIdx + 1);
                if (mirrorRow) mirrorRow.reformat();
            }
            state.markDirty();
        });

        const countEl = document.getElementById("saa-asset-class-count");
        if (countEl) countEl.textContent = String(ids.length);
    }

    function wireConfiguration(root) {
        const bootstrap = readBootstrap();
        if (!bootstrap) return;

        const lookup = bootstrap.asset_class_lookup;
        const assetClassOptions = Object.values(lookup).map(function (ac) {
            return { label: ac.display_name, value: ac.id };
        }).sort(function (a, b) {
            return String(a.label).localeCompare(String(b.label));
        });

        // Dirty-state machinery
        let isDirty = false;
        const saveBtn = document.getElementById("saa-save-config-btn");
        const saveStatus = document.getElementById("saa-save-bar-status");
        const saveIndicator =
            root.querySelector(".save-bar__indicator");

        function markDirty() {
            if (isDirty) return;
            isDirty = true;
            if (saveBtn) saveBtn.disabled = false;
            if (saveStatus) {
                saveStatus.textContent = "Unsaved changes";
                saveStatus.classList.add("save-bar__status--dirty");
            }
            if (saveIndicator) saveIndicator.classList.add("dirty");
        }
        function markClean(message) {
            isDirty = false;
            if (saveBtn) saveBtn.disabled = true;
            if (saveStatus) {
                saveStatus.textContent = message || "All changes saved";
                saveStatus.classList.remove("save-bar__status--dirty");
                saveStatus.classList.remove("save-bar__status--error");
            }
            if (saveIndicator) saveIndicator.classList.remove("dirty");
        }

        ["saa-config-name", "saa-risk-free-rate", "saa-frontier-points"]
            .forEach(function (id) {
                const el = document.getElementById(id);
                if (el) el.addEventListener("input", markDirty);
            });

        // Inputs table
        const pctFormatter = function (cell) {
            const v = cell.getValue();
            if (v === null || v === undefined || v === "") return "";
            return Number(v).toFixed(2) + "%";
        };
        const inputsRowsInitial = (bootstrap.inputs || []).map(function (row) {
            return {
                id: row.id,
                asset_class_id: row.asset_class_id,
                expected_return_pct: row.expected_return * 100,
                volatility_pct: row.volatility * 100,
                min_weight_pct: row.min_weight * 100,
                max_weight_pct: row.max_weight * 100,
            };
        });

        if (inputsTable) { inputsTable.destroy(); inputsTable = null; }
        inputsTable = new Tabulator("#saa-asset-class-inputs-table", {
            data: inputsRowsInitial,
            layout: "fitColumns",
            reactiveData: true,
            index: "asset_class_id",
            columns: [
                {
                    title: "Asset Class",
                    field: "asset_class_id",
                    editor: "list",
                    editorParams: {
                        values: assetClassOptions,
                        autocomplete: true,
                        listOnEmpty: true,
                        placeholderEmpty: "Select an asset class",
                    },
                    formatter: function (cell) {
                        const v = cell.getValue();
                        return v
                            ? escapeHtml(nameForAssetClass(lookup, v))
                            : "<em>(unset)</em>";
                    },
                    validator: ["required"],
                    widthGrow: 2,
                },
                { title: "Exp. Return", field: "expected_return_pct",
                  editor: "number",
                  editorParams: { min: -50, max: 50, step: 0.01 },
                  formatter: pctFormatter, hozAlign: "right",
                  validator: ["required", "min:-50", "max:50"] },
                { title: "Volatility", field: "volatility_pct",
                  editor: "number",
                  editorParams: { min: 0, max: 200, step: 0.01 },
                  formatter: pctFormatter, hozAlign: "right",
                  validator: ["required", "min:0"] },
                { title: "Min Weight", field: "min_weight_pct",
                  editor: "number",
                  editorParams: { min: 0, max: 100, step: 0.1 },
                  formatter: pctFormatter, hozAlign: "right",
                  validator: ["required", "min:0", "max:100"] },
                { title: "Max Weight", field: "max_weight_pct",
                  editor: "number",
                  editorParams: { min: 0, max: 100, step: 0.1 },
                  formatter: pctFormatter, hozAlign: "right",
                  validator: ["required", "min:0", "max:100"] },
                {
                    title: "", field: "_delete", width: 60,
                    hozAlign: "center", headerSort: false,
                    formatter: function () {
                        return '<button type="button" class="saa-row-delete-btn"'
                            + ' aria-label="Delete row">×</button>';
                    },
                    cellClick: function (e, cell) {
                        if (!e.target.classList.contains("saa-row-delete-btn")) return;
                        if (!confirm("Remove this asset class from the configuration?"))
                            return;
                        cell.getRow().delete();
                    },
                },
            ],
        });

        const state = {
            inputsTable: inputsTable,
            assetClassLookup: lookup,
            initialCorrelations: bootstrap.correlations || [],
            markDirty: markDirty,
        };

        function rebuildPreservingCorrelations() {
            const preserved = preserveCorrelationsAsMap(matrixTable);
            buildCorrelationMatrix(state, preserved);
        }

        // Build the matrix synchronously from the locally-known initial
        // inputs. We cannot rely on inputsTable.getData() at this point —
        // Tabulator's tableBuilt event is unreliable here (the inputs table
        // uses reactiveData + index options that change the init sequence),
        // and getData() returns [] before the rows are committed.
        //
        // inputsRowsInitial is already populated from the bootstrap JSON
        // (see the .map() above this Tabulator constructor call), so we
        // derive the asset-class ids directly from it.
        const initialIds = inputsRowsInitial
            .map(function (row) { return row.asset_class_id; })
            .filter(function (id) { return !!id; });
        buildCorrelationMatrix(state, null, initialIds);

        inputsTable.on("rowAdded", function () {
            rebuildPreservingCorrelations(); markDirty();
        });
        inputsTable.on("rowDeleted", function () {
            rebuildPreservingCorrelations(); markDirty();
        });
        inputsTable.on("cellEdited", function (cell) {
            if (cell.getField() === "asset_class_id") {
                rebuildPreservingCorrelations();
            }
            markDirty();
        });
        inputsTable.on("validationFailed", function (cell) {
            cell.getElement().classList.add("saa-cell-validation-error");
            if (saveStatus) {
                saveStatus.textContent = "Fix the highlighted cell before saving.";
                saveStatus.classList.add("save-bar__status--error");
            }
        });

        const addBtn = document.getElementById("saa-add-input-row-btn");
        if (addBtn) {
            addBtn.addEventListener("click", function () {
                const usedIds = new Set(
                    inputsTable.getData()
                        .map(function (r) { return r.asset_class_id; })
                );
                const unused = assetClassOptions.find(function (o) {
                    return !usedIds.has(o.value);
                });
                inputsTable.addRow({
                    asset_class_id: unused ? unused.value : null,
                    expected_return_pct: 5.0,
                    volatility_pct: 10.0,
                    min_weight_pct: 0.0,
                    max_weight_pct: 100.0,
                });
            });
        }

        // Save handler
        if (saveBtn) {
            saveBtn.addEventListener("click", async function () {
                document.querySelectorAll(
                    ".saa-row-validation-error, .saa-cell-validation-error"
                ).forEach(function (el) {
                    el.classList.remove("saa-row-validation-error");
                    el.classList.remove("saa-cell-validation-error");
                });
                if (saveStatus) saveStatus.classList.remove("save-bar__status--error");

                const inputsRows = inputsTable.getData();
                const missingAC = inputsRows.findIndex(function (r) {
                    return !r.asset_class_id;
                });
                if (missingAC !== -1) {
                    if (saveStatus) {
                        saveStatus.textContent =
                            "Pick an asset class on every row before saving.";
                        saveStatus.classList.add("save-bar__status--error");
                    }
                    const row = inputsTable.getRowFromPosition(missingAC + 1);
                    if (row) row.getElement().classList.add("saa-row-validation-error");
                    return;
                }

                const inputsPayload = inputsRows.map(function (r) {
                    return {
                        asset_class_id: r.asset_class_id,
                        expected_return: Number(r.expected_return_pct) / 100,
                        volatility: Number(r.volatility_pct) / 100,
                        min_weight: Number(r.min_weight_pct) / 100,
                        max_weight: Number(r.max_weight_pct) / 100,
                    };
                });

                const ids = inputsRows
                    .map(function (r) { return r.asset_class_id; })
                    .filter(function (x) { return !!x; })
                    .slice();
                ids.sort(function (a, b) {
                    return nameForAssetClass(lookup, a)
                        .localeCompare(nameForAssetClass(lookup, b));
                });
                const correlationsPayload = [];
                const matrixData = matrixTable ? matrixTable.getData() : [];
                for (let i = 0; i < ids.length; i++) {
                    for (let j = i + 1; j < ids.length; j++) {
                        const value = matrixData[i]
                            ? matrixData[i]["col_" + j] : null;
                        if (value === null || value === undefined || value === "")
                            continue;
                        const num = Number(value);
                        if (Number.isNaN(num)) continue;
                        const aId = ids[i], bId = ids[j];
                        const ordered = aId < bId ? [aId, bId] : [bId, aId];
                        correlationsPayload.push({
                            asset_class_a_id: ordered[0],
                            asset_class_b_id: ordered[1],
                            correlation: num,
                        });
                    }
                }

                const payload = {
                    metadata: {
                        name: document.getElementById("saa-config-name").value.trim(),
                        risk_free_rate:
                            parseFloat(document.getElementById("saa-risk-free-rate").value)
                            / 100,
                        n_frontier_points:
                            parseInt(document.getElementById("saa-frontier-points").value, 10),
                    },
                    inputs: inputsPayload,
                    correlations: correlationsPayload,
                };

                saveBtn.disabled = true;
                if (saveStatus) saveStatus.textContent = "Saving …";

                const { response, body } = await fetchJson(
                    "/api/saa/configuration/" + encodeURIComponent(bootstrap.config_id),
                    {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload),
                    }
                );
                if (response.ok) {
                    markClean("Saved successfully");
                    setTimeout(function () {
                        if (!isDirty && saveStatus)
                            saveStatus.textContent = "All changes saved";
                    }, 3000);
                    return;
                }
                saveBtn.disabled = false;
                if (saveStatus) {
                    saveStatus.textContent = "Error: "
                        + ((body && body.error) || "save failed.");
                    saveStatus.classList.add("save-bar__status--error");
                }
                if (body && typeof body.row_index === "number") {
                    const row = inputsTable.getRowFromPosition(body.row_index + 1);
                    if (row) row.getElement().classList.add("saa-row-validation-error");
                }
            });
        }

        window.addEventListener("beforeunload", function (e) {
            if (!isDirty) return;
            e.preventDefault();
            e.returnValue = "";
            return "";
        });
    }

    // -----------------------------------------------------------------
    // Asset-classes modal
    // -----------------------------------------------------------------

    async function openAssetClassesModal() {
        const dlg = document.getElementById("saa-asset-classes-dialog");
        if (!dlg) return;
        if (typeof window.htmx === "undefined") return;
        await window.htmx.ajax("GET", "/api/saa/asset-classes", {
            target: "#saa-asset-classes-dialog",
            swap: "innerHTML",
        });
        wireAssetClassesModal(dlg);
        openDialog(dlg);
    }

    function readAssetClassesBootstrap() {
        const tag = document.getElementById("saa-asset-classes-bootstrap");
        if (!tag) return [];
        try { return JSON.parse(tag.textContent); }
        catch (e) { return []; }
    }

    async function updateAssetClass(id, fields) {
        const { response, body } = await fetchJson(
            "/api/saa/asset-classes/" + encodeURIComponent(id),
            {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(fields),
            }
        );
        return { response, body };
    }

    async function deleteAssetClass(id, usageCount) {
        if (usageCount > 0) {
            alert("This asset class is referenced by " + usageCount
                + " configuration" + (usageCount === 1 ? "" : "s")
                + " and cannot be deleted.");
            return;
        }
        if (!confirm("Delete this asset class? This cannot be undone.")) return;
        const { response, body } = await fetchJson(
            "/api/saa/asset-classes/" + encodeURIComponent(id),
            { method: "DELETE" }
        );
        if (response.ok) openAssetClassesModal();  // re-fetch the table
        else alert("Deletion failed: "
            + ((body && body.error) || response.status));
    }

    function wireAssetClassesModal(dlg) {
        dlg.querySelectorAll("[data-action='close-asset-classes']").forEach(function (btn) {
            btn.addEventListener("click", function () { closeDialog(dlg); });
        });

        const data = readAssetClassesBootstrap();
        if (assetClassesTable) {
            assetClassesTable.destroy();
            assetClassesTable = null;
        }
        assetClassesTable = new Tabulator("#saa-asset-classes-table", {
            data: data,
            layout: "fitColumns",
            index: "id",
            columns: [
                {
                    title: "Code", field: "code", widthGrow: 1,
                    formatter: function (cell) {
                        return "<code>" + escapeHtml(cell.getValue() || "") + "</code>";
                    },
                },
                {
                    title: "Display Name", field: "display_name",
                    editor: "input", widthGrow: 2,
                    validator: ["required"],
                },
                {
                    title: "Description", field: "description",
                    editor: "input", widthGrow: 3,
                },
                {
                    title: "Used by", field: "usage_count",
                    hozAlign: "right", width: 100,
                    formatter: function (cell) {
                        const v = cell.getValue();
                        return v + " config" + (v === 1 ? "" : "s");
                    },
                },
                {
                    title: "Last Updated", field: "updated_at", width: 200,
                    formatter: function (cell) {
                        const raw = cell.getValue();
                        if (!raw) return "";
                        const d = new Date(raw);
                        return isNaN(d.getTime())
                            ? String(raw) : d.toLocaleString();
                    },
                },
                {
                    title: "Actions", field: "_actions", width: 110,
                    hozAlign: "center", headerSort: false,
                    formatter: function (cell) {
                        const row = cell.getRow().getData();
                        const enabled = row.usage_count === 0;
                        return '<button type="button"'
                            + ' class="saa-row-action-btn saa-row-action-btn--delete"'
                            + ' data-id="' + row.id + '"'
                            + ' data-usage="' + row.usage_count + '"'
                            + (enabled ? '' : ' disabled')
                            + '>Delete</button>';
                    },
                    cellClick: function (e, cell) {
                        const target = e.target;
                        if (!target.classList.contains("saa-row-action-btn--delete"))
                            return;
                        if (target.disabled) return;
                        const id = target.getAttribute("data-id");
                        const usage = parseInt(target.getAttribute("data-usage"), 10) || 0;
                        deleteAssetClass(id, usage);
                    },
                },
            ],
            initialSort: [{ column: "display_name", dir: "asc" }],
        });

        assetClassesTable.on("cellEdited", async function (cell) {
            const row = cell.getRow().getData();
            const field = cell.getField();
            if (field !== "display_name" && field !== "description") return;
            const fields = {};
            fields[field] = cell.getValue();
            const { response, body } = await updateAssetClass(row.id, fields);
            if (!response.ok) {
                cell.restoreOldValue();
                alert("Update failed: "
                    + ((body && body.error) || response.status));
            }
        });

        assetClassesTable.on("validationFailed", function (cell) {
            cell.getElement().classList.add("saa-cell-validation-error");
        });

        const newForm = document.getElementById("saa-new-ac-form");
        if (newForm) {
            newForm.addEventListener("submit", async function (evt) {
                evt.preventDefault();
                const errEl = document.getElementById("saa-new-ac-error");
                if (errEl) { errEl.hidden = true; errEl.textContent = ""; }
                const formData = new FormData(newForm);
                const { response, body } = await fetchJson(
                    "/api/saa/asset-classes",
                    { method: "POST", body: formData }
                );
                if (response.ok) {
                    newForm.reset();
                    openAssetClassesModal();
                    return;
                }
                if (errEl) {
                    errEl.hidden = false;
                    errEl.textContent = (body && body.error)
                        || ("Create failed (" + response.status + ").");
                }
            });
        }
    }

    // -----------------------------------------------------------------
    // Initialisation
    // -----------------------------------------------------------------

    function initSection() {
        const root = document.getElementById("pf-saa-root");
        if (!root) return;
        wirePicker(root);
        wireNewConfigDialog();
        if (document.getElementById("saa-config-bootstrap")) {
            wireConfiguration(root);
        }
        maybeFollowFragment();
    }

    function initConfigBodyOnly() {
        // Re-wire after the picker switches to a different config
        // (only #saa-config-body was swapped, not the whole section).
        const root = document.getElementById("pf-saa-root");
        if (!root) return;
        if (document.getElementById("saa-config-bootstrap")) {
            wireConfiguration(root);
        }
    }

    // Bind once on script load; re-bind on HTMX swaps.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initSection);
    } else {
        initSection();
    }

    document.body.addEventListener("htmx:afterSwap", function (evt) {
        if (!evt || !evt.target) return;
        if (evt.target.id === "pf-saa-root") {
            initSection();
        } else if (evt.target.id === "saa-config-body") {
            initConfigBodyOnly();
        }
    });

    // Event-driven coordination with backend mutations.
    document.body.addEventListener("pf:saa-asset-class-deleted", function () {
        openAssetClassesModal();
    });
})();
