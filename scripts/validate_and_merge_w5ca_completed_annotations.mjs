import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import {
  FileBlob,
  SpreadsheetFile,
  Workbook,
} from "@oai/artifact-tool";


const ROOT = process.cwd();
const INTERIM = path.join(
  ROOT,
  "data",
  "amazon_reviews_2023",
  "interim",
  "w5c_a",
);
const REPORTS = path.join(
  ROOT,
  "data",
  "amazon_reviews_2023",
  "reports",
  "w5c_a",
);
const QA_DIR = process.env.W5CA_RETURN_QA_DIR;
if (!QA_DIR) {
  throw new Error("W5CA_RETURN_QA_DIR must point to a temporary QA directory");
}

const OUTPUT_XLSX = path.join(
  INTERIM,
  "w5c_a_double_review_240_merged_for_adjudication.xlsx",
);
const VALIDATION_JSON = path.join(
  REPORTS,
  "annotation_return_validation.json",
);
const ISSUES_CSV = path.join(REPORTS, "annotation_return_issues.csv");
const STATUS_JSON = path.join(REPORTS, "w5c_a_annotation_return_status.json");

const INPUTS = [];
for (let batch = 1; batch <= 4; batch += 1) {
  INPUTS.push({
    batch,
    reviewer: 1,
    expectedRows: 300,
    originalCsv: path.join(
      INTERIM,
      `annotation_batch_${batch}_reviewer1_blind.csv`,
    ),
    completedXlsx: path.join(
      INTERIM,
      `annotation_batch_${batch}_reviewer1_completed.xlsx`,
    ),
  });
  INPUTS.push({
    batch,
    reviewer: 2,
    expectedRows: 60,
    originalCsv: path.join(
      INTERIM,
      `annotation_batch_${batch}_reviewer2_blind.csv`,
    ),
    completedXlsx: path.join(
      INTERIM,
      `annotation_batch_${batch}_reviewer2_blind_completed.xlsx`,
    ),
  });
}

const ALLOWED_BINARY = new Set(["0", "1", "uncertain"]);
const ALLOWED_TYPE = new Set([
  "N0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
]);
const ALLOWED_SEVERITY = new Set(["0", "1", "2", "3"]);
const ALLOWED_PERSISTENCE = new Set(["0", "1", "2"]);
const ALLOWED_CONFIDENCE = new Set(["low", "medium", "high"]);

function cell(value) {
  return String(value ?? "").trim();
}

function header(value) {
  return cell(value).replace(/^\uFEFF/, "");
}

function csvEscape(value) {
  const text = String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

async function sha256(filePath) {
  const data = await fs.readFile(filePath);
  return crypto.createHash("sha256").update(data).digest("hex");
}

async function fileIdentity(filePath) {
  const stats = await fs.stat(filePath);
  return {
    path: path.relative(ROOT, filePath),
    size_bytes: stats.size,
    mtime_utc: stats.mtime.toISOString(),
    sha256: await sha256(filePath),
  };
}

function pushIssue(issues, item, blindId, field, code, value = "") {
  issues.push({
    batch_id: `batch_${item.batch}`,
    reviewer: item.reviewer,
    blind_review_id: blindId,
    field,
    issue_code: code,
    observed_value: value,
  });
}

function typeCodes(value) {
  if (!value) return [];
  return value.split(";").map((part) => part.trim()).filter(Boolean);
}

function canonicalType(value) {
  return [...new Set(typeCodes(value))].sort().join(";");
}

function validateIndependentLabels(issues, item, record, prefix) {
  const blindId = record.blind_review_id;
  const binary = cell(record[`${prefix}_failure_binary`]).toLowerCase();
  const failureType = cell(record[`${prefix}_failure_type`]).toUpperCase();
  const severity = cell(record[`${prefix}_severity`]);
  const persistence = cell(record[`${prefix}_persistence`]);
  const confidence = cell(record[`${prefix}_confidence`]).toLowerCase();

  if (!binary) {
    pushIssue(
      issues, item, blindId, `${prefix}_failure_binary`, "MISSING_REQUIRED",
    );
  } else if (!ALLOWED_BINARY.has(binary)) {
    pushIssue(
      issues,
      item,
      blindId,
      `${prefix}_failure_binary`,
      "INVALID_VALUE",
      binary,
    );
  }
  if (!confidence) {
    pushIssue(
      issues, item, blindId, `${prefix}_confidence`, "MISSING_REQUIRED",
    );
  } else if (!ALLOWED_CONFIDENCE.has(confidence)) {
    pushIssue(
      issues,
      item,
      blindId,
      `${prefix}_confidence`,
      "INVALID_VALUE",
      confidence,
    );
  }

  if (failureType.includes(",")) {
    pushIssue(
      issues,
      item,
      blindId,
      `${prefix}_failure_type`,
      "USE_SEMICOLON_DELIMITER",
      failureType,
    );
  }
  const codes = typeCodes(failureType);
  const invalidCodes = codes.filter((code) => !ALLOWED_TYPE.has(code));
  if (invalidCodes.length) {
    pushIssue(
      issues,
      item,
      blindId,
      `${prefix}_failure_type`,
      "INVALID_FAILURE_TYPE",
      invalidCodes.join(";"),
    );
  }

  if (binary === "0") {
    if (failureType !== "N0") {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_failure_type`,
        "BINARY_0_REQUIRES_N0",
        failureType,
      );
    }
    if (severity !== "0") {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_severity`,
        "BINARY_0_REQUIRES_SEVERITY_0",
        severity,
      );
    }
    if (persistence !== "0") {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_persistence`,
        "BINARY_0_REQUIRES_PERSISTENCE_0",
        persistence,
      );
    }
  } else if (binary === "1") {
    if (!failureType || codes.includes("N0")) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_failure_type`,
        "BINARY_1_REQUIRES_F1_F8",
        failureType,
      );
    }
    if (!new Set(["1", "2", "3"]).has(severity)) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_severity`,
        "BINARY_1_REQUIRES_SEVERITY_1_3",
        severity,
      );
    }
    if (!ALLOWED_PERSISTENCE.has(persistence)) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_persistence`,
        "INVALID_OR_MISSING_PERSISTENCE",
        persistence,
      );
    }
  } else if (binary === "uncertain") {
    if (failureType && invalidCodes.length === 0 && codes.includes("N0")) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_failure_type`,
        "UNCERTAIN_SHOULD_NOT_USE_N0",
        failureType,
      );
    }
    if (severity && !ALLOWED_SEVERITY.has(severity)) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_severity`,
        "INVALID_VALUE",
        severity,
      );
    }
    if (persistence && !ALLOWED_PERSISTENCE.has(persistence)) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_persistence`,
        "INVALID_VALUE",
        persistence,
      );
    }
  } else {
    if (failureType && invalidCodes.length === 0) {
      // Preserve the independent issue on binary without creating noise here.
    }
    if (severity && !ALLOWED_SEVERITY.has(severity)) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_severity`,
        "INVALID_VALUE",
        severity,
      );
    }
    if (persistence && !ALLOWED_PERSISTENCE.has(persistence)) {
      pushIssue(
        issues,
        item,
        blindId,
        `${prefix}_persistence`,
        "INVALID_VALUE",
        persistence,
      );
    }
  }
}

async function loadOriginalCsv(filePath) {
  const csvText = (await fs.readFile(filePath, "utf8")).replace(/^\uFEFF/, "");
  const workbook = await Workbook.fromCSV(csvText, { sheetName: "Annotation" });
  return workbook.worksheets.getItem("Annotation").getUsedRange().values;
}

async function loadCompleted(item) {
  const workbook = await SpreadsheetFile.importXlsx(
    await FileBlob.load(item.completedXlsx),
  );
  const dumpPath = `${item.completedXlsx}.inspect.ndjson`;
  try {
    await fs.rename(
      dumpPath,
      path.join(
        QA_DIR,
        `${path.basename(item.completedXlsx, ".xlsx")}_inspect.ndjson`,
      ),
    );
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const sheetInspection = await workbook.inspect({
    kind: "sheet",
    include: "id,name",
    maxChars: 2000,
  });
  const annotation = workbook.worksheets.getItem("Annotation");
  const values = annotation.getUsedRange().values;
  let formulas = 0;
  for (const row of annotation.getUsedRange().formulas ?? []) {
    for (const value of row ?? []) {
      if (typeof value === "string" && value.startsWith("=")) formulas += 1;
    }
  }
  const lastColumn = item.reviewer === 1 ? "N" : "I";
  const annotationPreview = await workbook.render({
    sheetName: "Annotation",
    range: `A1:${lastColumn}9`,
    scale: 1,
    format: "png",
  });
  const instructionsPreview = await workbook.render({
    sheetName: "Instructions",
    range: "A1:B12",
    scale: 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(
      QA_DIR,
      `${path.basename(item.completedXlsx, ".xlsx")}_annotation.png`,
    ),
    new Uint8Array(await annotationPreview.arrayBuffer()),
  );
  await fs.writeFile(
    path.join(
      QA_DIR,
      `${path.basename(item.completedXlsx, ".xlsx")}_instructions.png`,
    ),
    new Uint8Array(await instructionsPreview.arrayBuffer()),
  );
  return { workbook, values, formulas, sheetInspection };
}

function rowsToRecords(values) {
  const headers = values[0].map(header);
  return {
    headers,
    records: values.slice(1).map((row) =>
      Object.fromEntries(
        headers.map((name, index) => [name, cell(row[index])]),
      )),
  };
}

function columnLetter(indexZeroBased) {
  let value = indexZeroBased + 1;
  let output = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    output = String.fromCharCode(65 + remainder) + output;
    value = Math.floor((value - 1) / 26);
  }
  return output;
}

function styleTable(sheet, rows, columns, reviewTextColumn = 2) {
  const lastColumn = columnLetter(columns - 1);
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(2);
  sheet.getRange(`A1:${lastColumn}${rows + 1}`).format = {
    font: { name: "Calibri", size: 10, color: "#1F2937" },
    verticalAlignment: "top",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: "#1F4E78",
    font: { name: "Calibri", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    rowHeight: 42,
  };
  sheet.getRange(`A2:${lastColumn}${rows + 1}`).format.rowHeight = 66;
  sheet.getRange(`A1:A${rows + 1}`).format.columnWidth = 19;
  sheet.getRange(`B1:B${rows + 1}`).format.columnWidth = 15;
  const reviewLetter = columnLetter(reviewTextColumn);
  sheet.getRange(
    `${reviewLetter}1:${reviewLetter}${rows + 1}`,
  ).format.columnWidth = 72;
  for (let index = 3; index < columns; index += 1) {
    const letter = columnLetter(index);
    sheet.getRange(`${letter}1:${letter}${rows + 1}`).format.columnWidth =
      index === columns - 1 ? 30 : 18;
  }
}

async function buildAdjudicationWorkbook(doubleRows, summaryRows) {
  const workbook = Workbook.create();
  const summary = workbook.worksheets.add("Summary");
  summary.showGridLines = false;
  summary.getRange(`A1:B${summaryRows.length}`).values = summaryRows;
  summary.getRange(`A1:B${summaryRows.length}`).format = {
    font: { name: "Calibri", size: 11, color: "#1F2937" },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  summary.getRange("A1:B1").format = {
    fill: "#1F4E78",
    font: { name: "Calibri", size: 14, bold: true, color: "#FFFFFF" },
    rowHeight: 32,
  };
  summary.getRange(`A2:A${summaryRows.length}`).format = {
    fill: "#D9EAF7",
    font: { bold: true, color: "#17365D" },
  };
  summary.getRange(`A1:A${summaryRows.length}`).format.columnWidth = 34;
  summary.getRange(`B1:B${summaryRows.length}`).format.columnWidth = 82;
  summary.getRange(`A2:B${summaryRows.length}`).format.rowHeight = 38;

  const instructions = workbook.worksheets.add("Instructions");
  const instructionsRows = [
    ["W5-C-A Adjudication", "Use both independent labels and review_text."],
    [
      "Task",
      "Complete every adjudicated_* field for all 240 double-reviewed rows.",
    ],
    [
      "Do not change",
      "Do not edit Reviewer 1 or Reviewer 2 labels. Resolve differences only in adjudicated fields.",
    ],
    [
      "failure_binary",
      "Use 0, 1, or uncertain. Low rating is not failure evidence.",
    ],
    [
      "failure_type",
      "Use N0 only with binary 0. For failures, use F1–F8 separated by semicolons.",
    ],
    [
      "severity",
      "0 no failure; 1 minor/recoverable; 2 core loss/repeated/return; 3 safety or permanent/property risk.",
    ],
    [
      "persistence",
      "0 single/unknown; 1 intermittent/repeated; 2 continuous or unresolved after a remedy.",
    ],
    [
      "Notes",
      "Briefly explain disagreements or uncertain decisions. Do not consult hidden ratings or model signals.",
    ],
  ];
  instructions.getRange(`A1:B${instructionsRows.length}`).values =
    instructionsRows;
  instructions.getRange(`A1:B${instructionsRows.length}`).format = {
    font: { name: "Calibri", size: 11, color: "#1F2937" },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  instructions.getRange("A1:B1").format = {
    fill: "#1F4E78",
    font: { name: "Calibri", size: 14, bold: true, color: "#FFFFFF" },
    rowHeight: 32,
  };
  instructions.getRange(`A2:A${instructionsRows.length}`).format = {
    fill: "#D9EAF7",
    font: { bold: true, color: "#17365D" },
  };
  instructions.getRange(`A1:A${instructionsRows.length}`).format.columnWidth =
    28;
  instructions.getRange(`B1:B${instructionsRows.length}`).format.columnWidth =
    92;
  instructions.getRange(`A2:B${instructionsRows.length}`).format.rowHeight =
    44;

  const adjudication = workbook.worksheets.add("Adjudication");
  const headers = [
    "blind_review_id",
    "device_type",
    "review_text",
    "reviewer_1_failure_binary",
    "reviewer_1_failure_type",
    "reviewer_1_severity",
    "reviewer_1_persistence",
    "reviewer_1_confidence",
    "reviewer_1_notes",
    "reviewer_2_failure_binary",
    "reviewer_2_failure_type",
    "reviewer_2_severity",
    "reviewer_2_persistence",
    "reviewer_2_confidence",
    "reviewer_2_notes",
    "binary_agreement",
    "type_agreement",
    "severity_agreement",
    "persistence_agreement",
    "any_core_disagreement",
    "adjudicated_failure_binary",
    "adjudicated_failure_type",
    "adjudicated_severity",
    "adjudicated_persistence",
    "adjudication_notes",
  ];
  adjudication.getRangeByIndexes(
    0, 0, doubleRows.length + 1, headers.length,
  ).values = [headers, ...doubleRows.map((row) => headers.map(
    (name) => row[name] ?? "",
  ))];
  styleTable(adjudication, doubleRows.length, headers.length);
  const lastRow = doubleRows.length + 1;
  adjudication.getRange(`P2:T${lastRow}`).format = {
    fill: "#FFF2CC",
    font: { name: "Calibri", size: 10, color: "#7F6000" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  adjudication.getRange(`U2:Y${lastRow}`).format = {
    fill: "#E2F0D9",
    font: { name: "Calibri", size: 10, color: "#375623" },
    verticalAlignment: "top",
    wrapText: true,
  };
  adjudication.getRange(`U2:U${lastRow}`).dataValidation = {
    rule: { type: "list", values: ["0", "1", "uncertain"] },
  };
  adjudication.getRange(`W2:W${lastRow}`).dataValidation = {
    rule: { type: "list", values: ["0", "1", "2", "3"] },
  };
  adjudication.getRange(`X2:X${lastRow}`).dataValidation = {
    rule: { type: "list", values: ["0", "1", "2"] },
  };

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(OUTPUT_XLSX);
  const imported = await SpreadsheetFile.importXlsx(
    await FileBlob.load(OUTPUT_XLSX),
  );
  const dumpPath = `${OUTPUT_XLSX}.inspect.ndjson`;
  try {
    await fs.rename(
      dumpPath,
      path.join(QA_DIR, "merged_adjudication_inspect.ndjson"),
    );
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const importedValues = imported.worksheets
    .getItem("Adjudication")
    .getUsedRange()
    .values;
  if (importedValues.length !== 241 || importedValues[0].length !== 25) {
    throw new Error("Merged adjudication workbook shape is invalid");
  }
  for (let rowIndex = 1; rowIndex < importedValues.length; rowIndex += 1) {
    for (let columnIndex = 20; columnIndex < 25; columnIndex += 1) {
      if (cell(importedValues[rowIndex][columnIndex]) !== "") {
        throw new Error("An adjudication field was prefilled");
      }
    }
  }
  let outputFormulaCount = 0;
  for (const sheetName of ["Summary", "Instructions", "Adjudication"]) {
    const formulas = imported.worksheets.getItem(sheetName).getUsedRange().formulas;
    outputFormulaCount += formulas.flat().filter((value) => cell(value) !== "").length;
  }
  const formulaErrorScan = await imported.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "Merged adjudication workbook formula error scan",
    maxChars: 3000,
  });
  await imported.inspect({
    kind: "region",
    sheetId: "Adjudication",
    range: "A1:Y6",
    summary: "Merged adjudication workbook key range inspection",
    maxChars: 3000,
  });
  const renderSpecs = [
    ["Summary", `A1:B${summaryRows.length}`, "merged_summary.png"],
    ["Instructions", "A1:B8", "merged_instructions.png"],
    ["Adjudication", "A1:Y8", "merged_adjudication.png"],
  ];
  for (const [sheetName, range, fileName] of renderSpecs) {
    const preview = await imported.render({
      sheetName,
      range,
      scale: 1,
      format: "png",
    });
    await fs.writeFile(
      path.join(QA_DIR, fileName),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
  return {
    row_count: importedValues.length - 1,
    column_count: importedValues[0].length,
    adjudication_fields_blank: true,
    formula_count: outputFormulaCount,
    formula_error_scan: formulaErrorScan,
    rendered_sheets: renderSpecs.map(([sheetName]) => sheetName),
  };
}

await fs.mkdir(REPORTS, { recursive: true });
await fs.mkdir(QA_DIR, { recursive: true });

const issues = [];
const inputManifest = [];
const independentRecords = new Map();
const r1AllIds = new Set();
const r2AllIds = new Set();
let formulaCount = 0;

for (const item of INPUTS) {
  const originalValues = await loadOriginalCsv(item.originalCsv);
  const completed = await loadCompleted(item);
  formulaCount += completed.formulas;
  const original = rowsToRecords(originalValues);
  const returned = rowsToRecords(completed.values);
  inputManifest.push({
    batch_id: `batch_${item.batch}`,
    reviewer: item.reviewer,
    expected_rows: item.expectedRows,
    returned_rows: returned.records.length,
    original_csv: await fileIdentity(item.originalCsv),
    completed_xlsx: await fileIdentity(item.completedXlsx),
    formulas_detected: completed.formulas,
    sheet_inspection: completed.sheetInspection,
  });

  if (
    returned.records.length !== item.expectedRows
    || original.records.length !== item.expectedRows
  ) {
    pushIssue(
      issues,
      item,
      "",
      "workbook",
      "ROW_COUNT_MISMATCH",
      `${returned.records.length}/${item.expectedRows}`,
    );
  }
  if (JSON.stringify(returned.headers) !== JSON.stringify(original.headers)) {
    pushIssue(
      issues,
      item,
      "",
      "workbook",
      "HEADER_MISMATCH",
      returned.headers.join("|"),
    );
  }
  const originalById = new Map(
    original.records.map((record) => [record.blind_review_id, record]),
  );
  const seen = new Set();
  for (const record of returned.records) {
    const blindId = record.blind_review_id;
    if (!blindId) {
      pushIssue(issues, item, "", "blind_review_id", "MISSING_ID");
      continue;
    }
    if (seen.has(blindId)) {
      pushIssue(
        issues, item, blindId, "blind_review_id", "DUPLICATE_WITHIN_FILE",
      );
    }
    seen.add(blindId);
    const originalRecord = originalById.get(blindId);
    if (!originalRecord) {
      pushIssue(
        issues, item, blindId, "blind_review_id", "UNEXPECTED_BLIND_ID",
      );
      continue;
    }
    for (const field of ["device_type", "review_text"]) {
      if (record[field] !== originalRecord[field]) {
        pushIssue(
          issues,
          item,
          blindId,
          field,
          "BLIND_SOURCE_VALUE_CHANGED",
        );
      }
    }
    const prefix = `reviewer_${item.reviewer}`;
    validateIndependentLabels(issues, item, record, prefix);
    if (item.reviewer === 1) {
      for (const field of [
        "adjudicated_failure_binary",
        "adjudicated_failure_type",
        "adjudicated_severity",
        "adjudicated_persistence",
        "adjudication_notes",
      ]) {
        if (cell(record[field]) !== "") {
          pushIssue(
            issues,
            item,
            blindId,
            field,
            "ADJUDICATION_PREFILLED",
            cell(record[field]),
          );
        }
      }
      if (r1AllIds.has(blindId)) {
        pushIssue(
          issues, item, blindId, "blind_review_id", "DUPLICATE_ACROSS_R1",
        );
      }
      r1AllIds.add(blindId);
    } else {
      if (r2AllIds.has(blindId)) {
        pushIssue(
          issues, item, blindId, "blind_review_id", "DUPLICATE_ACROSS_R2",
        );
      }
      r2AllIds.add(blindId);
    }
    independentRecords.set(
      `${item.reviewer}|${blindId}`,
      { ...record, batch: item.batch },
    );
  }
  for (const blindId of originalById.keys()) {
    if (!seen.has(blindId)) {
      pushIssue(
        issues, item, blindId, "blind_review_id", "EXPECTED_ID_MISSING",
      );
    }
  }
}

if (r1AllIds.size !== 1200) {
  issues.push({
    batch_id: "all",
    reviewer: 1,
    blind_review_id: "",
    field: "blind_review_id",
    issue_code: "R1_TOTAL_UNIQUE_ID_MISMATCH",
    observed_value: String(r1AllIds.size),
  });
}
if (r2AllIds.size !== 240) {
  issues.push({
    batch_id: "all",
    reviewer: 2,
    blind_review_id: "",
    field: "blind_review_id",
    issue_code: "R2_TOTAL_UNIQUE_ID_MISMATCH",
    observed_value: String(r2AllIds.size),
  });
}
for (const blindId of r2AllIds) {
  if (!r1AllIds.has(blindId)) {
    issues.push({
      batch_id: "all",
      reviewer: 2,
      blind_review_id: blindId,
      field: "blind_review_id",
      issue_code: "R2_ID_NOT_PRESENT_IN_R1",
      observed_value: "",
    });
  }
}
if (formulaCount > 0) {
  issues.push({
    batch_id: "all",
    reviewer: 0,
    blind_review_id: "",
    field: "workbook",
    issue_code: "UNEXPECTED_FORMULAS",
    observed_value: String(formulaCount),
  });
}

const binaryCounts = {};
for (const reviewer of [1, 2]) {
  binaryCounts[`reviewer_${reviewer}`] = {};
  for (const [key, record] of independentRecords.entries()) {
    if (!key.startsWith(`${reviewer}|`)) continue;
    const value = cell(record[`reviewer_${reviewer}_failure_binary`])
      .toLowerCase();
    binaryCounts[`reviewer_${reviewer}`][value] =
      (binaryCounts[`reviewer_${reviewer}`][value] ?? 0) + 1;
  }
}

const doubleRows = [];
const agreement = {
  compared_rows: 0,
  failure_binary_agree: 0,
  failure_type_agree: 0,
  severity_agree: 0,
  persistence_agree: 0,
  all_core_fields_agree: 0,
  rows_requiring_adjudication: 0,
};
for (const blindId of [...r2AllIds].sort()) {
  const r1 = independentRecords.get(`1|${blindId}`);
  const r2 = independentRecords.get(`2|${blindId}`);
  if (!r1 || !r2) continue;
  if (
    r1.device_type !== r2.device_type
    || r1.review_text !== r2.review_text
  ) {
    issues.push({
      batch_id: `batch_${r1.batch}`,
      reviewer: 0,
      blind_review_id: blindId,
      field: "review_identity",
      issue_code: "R1_R2_SOURCE_MISMATCH",
      observed_value: "",
    });
    continue;
  }
  const binaryAgree = (
    cell(r1.reviewer_1_failure_binary).toLowerCase()
    === cell(r2.reviewer_2_failure_binary).toLowerCase()
  );
  const typeAgree = (
    canonicalType(cell(r1.reviewer_1_failure_type).toUpperCase())
    === canonicalType(cell(r2.reviewer_2_failure_type).toUpperCase())
  );
  const severityAgree = (
    cell(r1.reviewer_1_severity) === cell(r2.reviewer_2_severity)
  );
  const persistenceAgree = (
    cell(r1.reviewer_1_persistence) === cell(r2.reviewer_2_persistence)
  );
  const allAgree = (
    binaryAgree && typeAgree && severityAgree && persistenceAgree
  );
  agreement.compared_rows += 1;
  if (binaryAgree) agreement.failure_binary_agree += 1;
  if (typeAgree) agreement.failure_type_agree += 1;
  if (severityAgree) agreement.severity_agree += 1;
  if (persistenceAgree) agreement.persistence_agree += 1;
  if (allAgree) agreement.all_core_fields_agree += 1;
  else agreement.rows_requiring_adjudication += 1;
  doubleRows.push({
    blind_review_id: blindId,
    device_type: r1.device_type,
    review_text: r1.review_text,
    reviewer_1_failure_binary: r1.reviewer_1_failure_binary,
    reviewer_1_failure_type: r1.reviewer_1_failure_type,
    reviewer_1_severity: r1.reviewer_1_severity,
    reviewer_1_persistence: r1.reviewer_1_persistence,
    reviewer_1_confidence: r1.reviewer_1_confidence,
    reviewer_1_notes: r1.reviewer_1_notes,
    reviewer_2_failure_binary: r2.reviewer_2_failure_binary,
    reviewer_2_failure_type: r2.reviewer_2_failure_type,
    reviewer_2_severity: r2.reviewer_2_severity,
    reviewer_2_persistence: r2.reviewer_2_persistence,
    reviewer_2_confidence: r2.reviewer_2_confidence,
    reviewer_2_notes: r2.reviewer_2_notes,
    binary_agreement: binaryAgree ? "agree" : "disagree",
    type_agreement: typeAgree ? "agree" : "disagree",
    severity_agreement: severityAgree ? "agree" : "disagree",
    persistence_agreement: persistenceAgree ? "agree" : "disagree",
    any_core_disagreement: allAgree ? "no" : "yes",
    adjudicated_failure_binary: "",
    adjudicated_failure_type: "",
    adjudicated_severity: "",
    adjudicated_persistence: "",
    adjudication_notes: "",
  });
}

const issueCodeCounts = {};
for (const issue of issues) {
  issueCodeCounts[issue.issue_code] =
    (issueCodeCounts[issue.issue_code] ?? 0) + 1;
}
const validation = {
  phase: "W5-C-A annotation return",
  validated_at_utc: new Date().toISOString(),
  status: issues.length ? "PAUSED_ANNOTATION_CORRECTION" : "PASS",
  completed_workbooks: inputManifest,
  reviewer_1_rows: r1AllIds.size,
  reviewer_2_rows: r2AllIds.size,
  reviewer_2_ids_are_subset_of_reviewer_1: [...r2AllIds].every(
    (id) => r1AllIds.has(id),
  ),
  formula_count: formulaCount,
  issue_count: issues.length,
  issue_code_counts: issueCodeCounts,
  binary_label_counts: binaryCounts,
  preliminary_double_review_agreement: agreement,
  notes: [
    "Agreement counts are descriptive return checks, not the final W5-C-B inter-annotator statistics.",
    "No label was interpreted, corrected, or adjudicated automatically.",
  ],
};
await fs.writeFile(
  VALIDATION_JSON,
  JSON.stringify(validation, null, 2),
  "utf8",
);
const issueHeaders = [
  "batch_id",
  "reviewer",
  "blind_review_id",
  "field",
  "issue_code",
  "observed_value",
];
const issueLines = [
  issueHeaders.map(csvEscape).join(","),
  ...issues.map((issue) =>
    issueHeaders.map((name) => csvEscape(issue[name])).join(",")),
];
await fs.writeFile(ISSUES_CSV, `${issueLines.join("\r\n")}\r\n`, "utf8");

let outputWorkbookValidation = null;
if (issues.length === 0) {
  const summaryRows = [
    ["W5-C-A return validation", "All eight completed workbooks passed."],
    ["Reviewer 1 rows", 1200],
    ["Reviewer 2 rows", 240],
    ["Failure binary agreement", `${agreement.failure_binary_agree}/240`],
    ["Failure type agreement", `${agreement.failure_type_agree}/240`],
    ["Severity agreement", `${agreement.severity_agree}/240`],
    ["Persistence agreement", `${agreement.persistence_agree}/240`],
    ["All four core fields agree", `${agreement.all_core_fields_agree}/240`],
    [
      "At least one core disagreement",
      `${agreement.rows_requiring_adjudication}/240`,
    ],
    [
      "Next action",
      "Complete the five green adjudication columns for every row.",
    ],
  ];
  outputWorkbookValidation = await buildAdjudicationWorkbook(
    doubleRows,
    summaryRows,
  );
  validation.output_workbook_validation = outputWorkbookValidation;
  await fs.writeFile(
    VALIDATION_JSON,
    JSON.stringify(validation, null, 2),
    "utf8",
  );
}

const status = {
  phase: "W5-C-A annotation return",
  status: issues.length
    ? "PAUSED_ANNOTATION_CORRECTION"
    : "PAUSED_HUMAN_ADJUDICATION",
  w5c_b_readiness: issues.length
    ? "WAITING_FOR_CORRECTED_ANNOTATION"
    : "WAITING_FOR_COMPLETED_ADJUDICATION",
  input_workbook_count: 8,
  reviewer_1_rows: r1AllIds.size,
  reviewer_2_rows: r2AllIds.size,
  issue_count: issues.length,
  labels_modified: false,
  labels_auto_filled: false,
  model_retrained: false,
  w6_executed: false,
  output_workbook_validation: outputWorkbookValidation,
  adjudication_workbook_created: issues.length === 0,
  adjudication_workbook: issues.length === 0
    ? await fileIdentity(OUTPUT_XLSX)
    : null,
};
await fs.writeFile(STATUS_JSON, JSON.stringify(status, null, 2), "utf8");
process.stdout.write(JSON.stringify({
  status: status.status,
  reviewer_1_rows: r1AllIds.size,
  reviewer_2_rows: r2AllIds.size,
  issue_count: issues.length,
  preliminary_double_review_agreement: agreement,
  adjudication_workbook_created: status.adjudication_workbook_created,
}));
