import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

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
const QA_DIR = process.env.W5CA_ADJUDICATION_QA_DIR;
if (!QA_DIR) {
  throw new Error("W5CA_ADJUDICATION_QA_DIR must be a temporary directory");
}

const TEMPLATE_XLSX = path.join(
  INTERIM,
  "w5c_a_double_review_240_merged_for_adjudication.xlsx",
);
const COMPLETED_XLSX = path.join(
  INTERIM,
  "w5c_a_double_review_240_merged_for_adjudication_completed.xlsx",
);
const VALIDATION_JSON = path.join(
  REPORTS,
  "w5c_a_adjudication_completion_validation.json",
);
const ISSUES_CSV = path.join(REPORTS, "w5c_a_adjudication_issues.csv");
const STATUS_JSON = path.join(REPORTS, "w5c_a_adjudication_status.json");

const EXPECTED_HEADERS = [
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
const ALLOWED_BINARY = new Set(["0", "1", "uncertain"]);
const ALLOWED_TYPE = new Set([
  "N0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
]);
const ALLOWED_SEVERITY = new Set(["0", "1", "2", "3"]);
const ALLOWED_PERSISTENCE = new Set(["0", "1", "2"]);

function cell(value) {
  return String(value ?? "").trim();
}

function typeCodes(value) {
  return cell(value)
    .toUpperCase()
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean);
}

function canonicalType(value) {
  return [...new Set(typeCodes(value))].sort().join(";");
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
  const stat = await fs.stat(filePath);
  return {
    path: path.relative(ROOT, filePath),
    size_bytes: stat.size,
    mtime_utc: stat.mtime.toISOString(),
    sha256: await sha256(filePath),
  };
}

function addIssue(issues, blindId, field, issueCode) {
  issues.push({
    blind_review_id: blindId,
    field,
    issue_code: issueCode,
  });
}

function countFormulas(workbook, sheetNames) {
  let count = 0;
  for (const name of sheetNames) {
    const formulas = workbook.worksheets.getItem(name).getUsedRange().formulas;
    count += formulas.flat().filter((value) => cell(value) !== "").length;
  }
  return count;
}

await fs.mkdir(REPORTS, { recursive: true });
await fs.mkdir(QA_DIR, { recursive: true });

const templateIdentityBefore = await fileIdentity(TEMPLATE_XLSX);
const completedIdentityBefore = await fileIdentity(COMPLETED_XLSX);
const templateWorkbook = await SpreadsheetFile.importXlsx(
  await FileBlob.load(TEMPLATE_XLSX),
);
const completedWorkbook = await SpreadsheetFile.importXlsx(
  await FileBlob.load(COMPLETED_XLSX),
);
const sheetNames = ["Summary", "Instructions", "Adjudication"];
const issues = [];

for (const sheetName of sheetNames) {
  try {
    templateWorkbook.worksheets.getItem(sheetName);
    completedWorkbook.worksheets.getItem(sheetName);
  } catch {
    addIssue(issues, "", sheetName, "REQUIRED_SHEET_MISSING");
  }
}

const templateValues = templateWorkbook.worksheets
  .getItem("Adjudication")
  .getUsedRange()
  .values;
const completedValues = completedWorkbook.worksheets
  .getItem("Adjudication")
  .getUsedRange()
  .values;

if (templateValues.length !== 241 || templateValues[0]?.length !== 25) {
  addIssue(issues, "", "Adjudication", "TEMPLATE_SHAPE_MISMATCH");
}
if (completedValues.length !== 241 || completedValues[0]?.length !== 25) {
  addIssue(issues, "", "Adjudication", "COMPLETED_SHAPE_MISMATCH");
}

const completedHeaders = (completedValues[0] ?? []).map(cell);
if (JSON.stringify(completedHeaders) !== JSON.stringify(EXPECTED_HEADERS)) {
  addIssue(issues, "", "Adjudication", "HEADER_MISMATCH");
}

let protectedCellDifferences = 0;
const seenIds = new Set();
const finalBinaryCounts = { "0": 0, "1": 0, uncertain: 0 };
const finalSeverityCounts = { "0": 0, "1": 0, "2": 0, "3": 0 };
const finalPersistenceCounts = { "0": 0, "1": 0, "2": 0 };
const finalFailureTypeCounts = Object.fromEntries(
  [...ALLOWED_TYPE].map((code) => [code, 0]),
);
let agreedRows = 0;
let disagreementRows = 0;
let agreedRowsMatchingCommonConclusion = 0;
let protocolCompleteRows = 0;
let rowsWithAllFiveCellsNonblank = 0;
let uncertainRowsWithOptionalFieldsBlank = 0;

for (let rowIndex = 1; rowIndex < Math.min(
  templateValues.length,
  completedValues.length,
); rowIndex += 1) {
  const templateRow = templateValues[rowIndex];
  const row = completedValues[rowIndex];
  const blindId = cell(row[0]) || cell(templateRow[0]);

  for (let columnIndex = 0; columnIndex < 20; columnIndex += 1) {
    if (JSON.stringify(row[columnIndex]) !== JSON.stringify(
      templateRow[columnIndex],
    )) {
      protectedCellDifferences += 1;
      addIssue(
        issues,
        blindId,
        EXPECTED_HEADERS[columnIndex],
        "PROTECTED_A_T_VALUE_CHANGED",
      );
    }
  }

  if (!blindId) {
    addIssue(issues, "", "blind_review_id", "MISSING_BLIND_ID");
  } else if (seenIds.has(blindId)) {
    addIssue(issues, blindId, "blind_review_id", "DUPLICATE_BLIND_ID");
  } else {
    seenIds.add(blindId);
  }

  const binary = cell(row[20]).toLowerCase();
  const failureType = cell(row[21]).toUpperCase();
  const severity = cell(row[22]);
  const persistence = cell(row[23]);
  const notes = cell(row[24]);

  for (const columnIndex of [20, 24]) {
    if (!cell(row[columnIndex])) {
      addIssue(
        issues,
        blindId,
        EXPECTED_HEADERS[columnIndex],
        "MISSING_ADJUDICATION_VALUE",
      );
    }
  }
  if ([binary, failureType, severity, persistence, notes].every(Boolean)) {
    rowsWithAllFiveCellsNonblank += 1;
  }
  if (
    binary === "uncertain"
    && [failureType, severity, persistence].some((value) => !value)
  ) {
    uncertainRowsWithOptionalFieldsBlank += 1;
  }
  if (binary !== "uncertain") {
    for (let columnIndex = 21; columnIndex < 24; columnIndex += 1) {
      if (!cell(row[columnIndex])) {
        addIssue(
          issues,
          blindId,
          EXPECTED_HEADERS[columnIndex],
          "MISSING_ADJUDICATION_VALUE",
        );
      }
    }
  }
  if (
    binary
    && notes
    && (
      binary === "uncertain"
      || (failureType && severity && persistence)
    )
  ) {
    protocolCompleteRows += 1;
  }

  if (!ALLOWED_BINARY.has(binary)) {
    addIssue(issues, blindId, "adjudicated_failure_binary", "INVALID_VALUE");
  } else {
    finalBinaryCounts[binary] += 1;
  }
  if (failureType.includes(",")) {
    addIssue(
      issues,
      blindId,
      "adjudicated_failure_type",
      "USE_SEMICOLON_DELIMITER",
    );
  }
  const codes = typeCodes(failureType);
  if (new Set(codes).size !== codes.length) {
    addIssue(
      issues,
      blindId,
      "adjudicated_failure_type",
      "DUPLICATE_FAILURE_TYPE_CODE",
    );
  }
  const invalidCodes = codes.filter((code) => !ALLOWED_TYPE.has(code));
  if (invalidCodes.length || (!failureType && binary !== "uncertain")) {
    addIssue(
      issues,
      blindId,
      "adjudicated_failure_type",
      "INVALID_FAILURE_TYPE",
    );
  } else if (failureType) {
    for (const code of new Set(codes)) finalFailureTypeCounts[code] += 1;
  }
  if (severity && !ALLOWED_SEVERITY.has(severity)) {
    addIssue(issues, blindId, "adjudicated_severity", "INVALID_VALUE");
  } else if (severity) {
    finalSeverityCounts[severity] += 1;
  }
  if (persistence && !ALLOWED_PERSISTENCE.has(persistence)) {
    addIssue(issues, blindId, "adjudicated_persistence", "INVALID_VALUE");
  } else if (persistence) {
    finalPersistenceCounts[persistence] += 1;
  }

  if (binary === "0") {
    if (failureType !== "N0") {
      addIssue(
        issues,
        blindId,
        "adjudicated_failure_type",
        "BINARY_0_REQUIRES_N0",
      );
    }
    if (severity !== "0") {
      addIssue(
        issues,
        blindId,
        "adjudicated_severity",
        "BINARY_0_REQUIRES_SEVERITY_0",
      );
    }
    if (persistence !== "0") {
      addIssue(
        issues,
        blindId,
        "adjudicated_persistence",
        "BINARY_0_REQUIRES_PERSISTENCE_0",
      );
    }
  } else if (binary === "1") {
    if (!codes.length || codes.includes("N0")) {
      addIssue(
        issues,
        blindId,
        "adjudicated_failure_type",
        "BINARY_1_REQUIRES_F1_F8",
      );
    }
    if (!new Set(["1", "2", "3"]).has(severity)) {
      addIssue(
        issues,
        blindId,
        "adjudicated_severity",
        "BINARY_1_REQUIRES_SEVERITY_1_3",
      );
    }
  } else if (binary === "uncertain" && codes.includes("N0")) {
    addIssue(
      issues,
      blindId,
      "adjudicated_failure_type",
      "UNCERTAIN_SHOULD_NOT_USE_N0",
    );
  }

  const disagreement = cell(row[19]).toLowerCase();
  if (disagreement === "no") {
    agreedRows += 1;
    const matchesCommonConclusion = (
      binary === cell(row[3]).toLowerCase()
      && canonicalType(failureType) === canonicalType(row[4])
      && severity === cell(row[5])
      && persistence === cell(row[6])
      && binary === cell(row[9]).toLowerCase()
      && canonicalType(failureType) === canonicalType(row[10])
      && severity === cell(row[11])
      && persistence === cell(row[12])
    );
    if (matchesCommonConclusion) agreedRowsMatchingCommonConclusion += 1;
    else {
      addIssue(
        issues,
        blindId,
        "adjudicated_core_fields",
        "AGREED_ROW_DOES_NOT_MATCH_COMMON_CONCLUSION",
      );
    }
  } else if (disagreement === "yes") {
    disagreementRows += 1;
  } else {
    addIssue(
      issues,
      blindId,
      "any_core_disagreement",
      "INVALID_AGREEMENT_FLAG",
    );
  }
}

if (seenIds.size !== 240) {
  addIssue(issues, "", "blind_review_id", "UNIQUE_ID_COUNT_MISMATCH");
}
if (agreedRows !== 170 || disagreementRows !== 70) {
  addIssue(issues, "", "any_core_disagreement", "AGREEMENT_COUNT_MISMATCH");
}

const formulaCountTemplate = countFormulas(templateWorkbook, sheetNames);
const formulaCountCompleted = countFormulas(completedWorkbook, sheetNames);
if (formulaCountCompleted !== formulaCountTemplate) {
  addIssue(issues, "", "workbook", "FORMULA_COUNT_CHANGED");
}

const formulaErrorScan = await completedWorkbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "Completed W5-C-A adjudication formula error scan",
  maxChars: 3000,
});
const formulaErrorMatchCount = formulaErrorScan.ndjson.includes(
  "matched 0 entries",
) ? 0 : 1;
if (formulaErrorMatchCount) {
  addIssue(issues, "", "workbook", "FORMULA_ERROR_TOKEN_FOUND");
}

await completedWorkbook.inspect({
  kind: "region",
  sheetId: "Adjudication",
  range: "A1:Y6",
  summary: "Completed adjudication key range inspection",
  maxChars: 2500,
});

const renderSpecs = [
  ["Summary", "A1:B10", "completed_summary.png"],
  ["Instructions", "A1:B8", "completed_instructions.png"],
  ["Adjudication", "A1:Y8", "completed_adjudication_head.png"],
  ["Adjudication", "A234:Y241", "completed_adjudication_tail.png"],
];
for (const [sheetName, range, fileName] of renderSpecs) {
  const preview = await completedWorkbook.render({
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

const issueCounts = {};
for (const issue of issues) {
  issueCounts[issue.issue_code] = (issueCounts[issue.issue_code] ?? 0) + 1;
}

const templateIdentityAfter = await fileIdentity(TEMPLATE_XLSX);
const completedIdentityAfter = await fileIdentity(COMPLETED_XLSX);
const validation = {
  phase: "W5-C-A completed adjudication validation",
  validated_at_utc: new Date().toISOString(),
  status: issues.length ? "PAUSED_ADJUDICATION_CORRECTION" : "PASS",
  template_workbook: templateIdentityAfter,
  completed_workbook: completedIdentityAfter,
  input_identity_unchanged_during_validation: {
    template: JSON.stringify(templateIdentityBefore) === JSON.stringify(
      templateIdentityAfter,
    ),
    completed: JSON.stringify(completedIdentityBefore) === JSON.stringify(
      completedIdentityAfter,
    ),
  },
  adjudication_sheet: {
    row_count: completedValues.length - 1,
    column_count: completedValues[0]?.length ?? 0,
    unique_blind_review_ids: seenIds.size,
    protected_a_t_cell_differences: protectedCellDifferences,
    protocol_complete_adjudication_rows: protocolCompleteRows,
    rows_with_all_five_cells_nonblank: rowsWithAllFiveCellsNonblank,
    uncertain_rows_with_optional_fields_blank: uncertainRowsWithOptionalFieldsBlank,
    agreed_rows: agreedRows,
    agreed_rows_matching_common_conclusion: agreedRowsMatchingCommonConclusion,
    disagreement_rows: disagreementRows,
  },
  final_label_counts: {
    failure_binary: finalBinaryCounts,
    failure_type_multilabel_occurrences: finalFailureTypeCounts,
    severity: finalSeverityCounts,
    persistence: finalPersistenceCounts,
  },
  workbook_quality: {
    template_formula_count: formulaCountTemplate,
    completed_formula_count: formulaCountCompleted,
    formula_error_matches: formulaErrorMatchCount,
    rendered_sheets: [...new Set(renderSpecs.map(([sheetName]) => sheetName))],
  },
  issue_count: issues.length,
  issue_code_counts: issueCounts,
  labels_modified_by_validation: false,
  model_retrained: false,
  w6_executed: false,
};
await fs.writeFile(
  VALIDATION_JSON,
  JSON.stringify(validation, null, 2),
  "utf8",
);

const issueHeaders = ["blind_review_id", "field", "issue_code"];
const issueLines = [
  issueHeaders.map(csvEscape).join(","),
  ...issues.map((issue) => issueHeaders.map(
    (name) => csvEscape(issue[name]),
  ).join(",")),
];
await fs.writeFile(ISSUES_CSV, `${issueLines.join("\r\n")}\r\n`, "utf8");

const status = {
  phase: "W5-C-A completed adjudication",
  status: issues.length
    ? "PAUSED_ADJUDICATION_CORRECTION"
    : "PAUSED_W5C_B_APPROVAL",
  w5c_b_readiness: issues.length
    ? "WAITING_FOR_CORRECTED_ADJUDICATION"
    : "READY_FOR_EXPLICIT_APPROVAL",
  completed_workbook: completedIdentityAfter,
  adjudicated_rows: completedValues.length - 1,
  protected_a_t_cell_differences: protectedCellDifferences,
  rows_with_complete_adjudication: protocolCompleteRows,
  rows_with_all_five_cells_nonblank: rowsWithAllFiveCellsNonblank,
  uncertain_rows_with_optional_fields_blank: uncertainRowsWithOptionalFieldsBlank,
  agreed_rows: agreedRows,
  disagreement_rows: disagreementRows,
  issue_count: issues.length,
  labels_modified: false,
  model_retrained: false,
  w6_executed: false,
};
await fs.writeFile(STATUS_JSON, JSON.stringify(status, null, 2), "utf8");

process.stdout.write(JSON.stringify({
  status: status.status,
  w5c_b_readiness: status.w5c_b_readiness,
  adjudicated_rows: status.adjudicated_rows,
  protected_a_t_cell_differences: protectedCellDifferences,
  rows_with_complete_adjudication: protocolCompleteRows,
  rows_with_all_five_cells_nonblank: rowsWithAllFiveCellsNonblank,
  uncertain_rows_with_optional_fields_blank: uncertainRowsWithOptionalFieldsBlank,
  agreed_rows: agreedRows,
  disagreement_rows: disagreementRows,
  final_binary_counts: finalBinaryCounts,
  issue_count: issues.length,
}));
