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
const PREVIEWS = process.env.W5CA_PREVIEW_DIR
  ?? path.join(REPORTS, "workbook_previews");

const EXPECTED = [];
for (let batch = 1; batch <= 4; batch += 1) {
  EXPECTED.push({
    batch,
    reviewer: 1,
    rows: 300,
    columns: 14,
    stem: `annotation_batch_${batch}_reviewer1_blind`,
  });
  EXPECTED.push({
    batch,
    reviewer: 2,
    rows: 60,
    columns: 9,
    stem: `annotation_batch_${batch}_reviewer2_blind`,
  });
}

const LABEL_DEFINITIONS = [
  ["W5-C-A Expanded Annotation", "Independent blind review instructions"],
  [
    "Scope",
    "Use only review_text. Hidden sampling signals must not be inferred or sought.",
  ],
  [
    "failure_binary = 1",
    "Explicit core-function failure or abnormal technical behavior.",
  ],
  [
    "failure_binary = 0",
    "No engineering failure, or only price, delivery, packaging, appearance, service, or another non-technical issue.",
  ],
  ["failure_binary = uncertain", "The text does not provide enough evidence."],
  [
    "failure_type",
    "Use N0 for non-failure. For failures, use one or more of F1–F8 separated by semicolons.",
  ],
  [
    "severity",
    "0 = no failure; 1 = minor/recoverable; 2 = core loss/repeated/return; 3 = safety, permanent damage, or property risk.",
  ],
  [
    "persistence",
    "0 = single/unknown; 1 = intermittent/repeated; 2 = continuous or unresolved after a remedy attempt.",
  ],
  ["confidence", "Use low, medium, or high."],
  [
    "Independence",
    "Reviewer 2 must finish the separate 60-row file without seeing Reviewer 1 decisions.",
  ],
  [
    "Do not use",
    "Do not use star rating, keyword/model signals, product identifiers, dates, or another reviewer's labels.",
  ],
  [
    "Important",
    "The sample targets difficult boundary cases and must not be used to estimate population failure prevalence directly.",
  ],
];

const HIDDEN_HEADERS = new Set([
  "rating",
  "low_star_indicator",
  "keyword_candidate_hit",
  "model_failure_probability",
  "model_uncertainty_distance",
  "parent_asin",
  "asin",
  "source_domain",
  "product_title",
  "review_datetime",
  "review_month",
  "user_id_hash",
  "duplicate_key",
]);

function labelColumns(headers) {
  return headers
    .map((header, index) => ({ header, index }))
    .filter(({ header }) =>
      header.startsWith("reviewer_")
      || header.startsWith("adjudicated_")
      || header.startsWith("adjudication_"))
    .map(({ index }) => index);
}

function excelColumn(indexZeroBased) {
  let value = indexZeroBased + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

async function buildOne(item) {
  const csvPath = path.join(INTERIM, `${item.stem}.csv`);
  const xlsxPath = path.join(INTERIM, `${item.stem}.xlsx`);
  const csvText = (await fs.readFile(csvPath, "utf8")).replace(/^\uFEFF/, "");
  const workbook = await Workbook.fromCSV(csvText, {
    sheetName: "Annotation",
  });
  const sheet = workbook.worksheets.getItem("Annotation");
  const used = sheet.getUsedRange();
  const values = used.values;
  if (!Array.isArray(values) || values.length !== item.rows + 1) {
    throw new Error(`${item.stem}: unexpected row count before export`);
  }
  const headers = values[0].map((value) => String(value ?? ""));
  if (headers.length !== item.columns) {
    throw new Error(`${item.stem}: unexpected column count before export`);
  }
  const exposed = headers.filter((header) => HIDDEN_HEADERS.has(header));
  if (exposed.length) {
    throw new Error(`${item.stem}: hidden fields exposed: ${exposed.join(", ")}`);
  }
  for (const columnIndex of labelColumns(headers)) {
    for (let rowIndex = 1; rowIndex < values.length; rowIndex += 1) {
      if (String(values[rowIndex][columnIndex] ?? "").trim() !== "") {
        throw new Error(`${item.stem}: human label cells are not blank`);
      }
    }
  }

  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(2);
  const lastColumn = excelColumn(item.columns - 1);
  const fullRange = sheet.getRange(`A1:${lastColumn}${item.rows + 1}`);
  fullRange.format = {
    font: { name: "Calibri", size: 10, color: "#1F2937" },
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: "#1F4E78",
    font: { name: "Calibri", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    rowHeight: 42,
    borders: { preset: "all", style: "thin", color: "#A6B8CE" },
  };
  sheet.getRange(`A2:${lastColumn}${item.rows + 1}`).format.rowHeight = 68;
  sheet.getRange(`A2:${lastColumn}${item.rows + 1}`).format.wrapText = true;
  sheet.getRange(`A2:B${item.rows + 1}`).format.verticalAlignment = "center";
  sheet.getRange(`A2:A${item.rows + 1}`).format.fill = "#EAF2F8";
  sheet.getRange(`B2:B${item.rows + 1}`).format.fill = "#F4F8FB";

  sheet.getRange(`A1:A${item.rows + 1}`).format.columnWidth = 19;
  sheet.getRange(`B1:B${item.rows + 1}`).format.columnWidth = 16;
  sheet.getRange(`C1:C${item.rows + 1}`).format.columnWidth = 78;
  for (let column = 3; column < item.columns; column += 1) {
    const header = headers[column];
    const columnLetter = excelColumn(column);
    const width = header.includes("notes") ? 30 : 19;
    sheet.getRange(
      `${columnLetter}1:${columnLetter}${item.rows + 1}`,
    ).format.columnWidth = width;
  }

  const listValidations = {
    failure_binary: ["0", "1", "uncertain"],
    severity: ["0", "1", "2", "3"],
    persistence: ["0", "1", "2"],
    confidence: ["low", "medium", "high"],
  };
  for (let index = 0; index < headers.length; index += 1) {
    const header = headers[index];
    for (const [suffix, valuesList] of Object.entries(listValidations)) {
      if (header.endsWith(suffix)) {
        const columnLetter = excelColumn(index);
        sheet.getRange(
          `${columnLetter}2:${columnLetter}${item.rows + 1}`,
        ).dataValidation = {
          rule: { type: "list", values: valuesList },
        };
      }
    }
  }

  const instructionSheet = workbook.worksheets.add("Instructions");
  instructionSheet.showGridLines = false;
  instructionSheet.getRange(`A1:B${LABEL_DEFINITIONS.length}`).values =
    LABEL_DEFINITIONS;
  instructionSheet.getRange(`A1:B${LABEL_DEFINITIONS.length}`).format = {
    font: { name: "Calibri", size: 11, color: "#1F2937" },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  instructionSheet.getRange("A1:B1").format = {
    fill: "#1F4E78",
    font: { name: "Calibri", size: 14, bold: true, color: "#FFFFFF" },
    rowHeight: 32,
  };
  instructionSheet.getRange(
    `A2:A${LABEL_DEFINITIONS.length}`,
  ).format = {
    fill: "#D9EAF7",
    font: { name: "Calibri", size: 11, bold: true, color: "#17365D" },
  };
  instructionSheet.getRange(
    `A1:A${LABEL_DEFINITIONS.length}`,
  ).format.columnWidth = 28;
  instructionSheet.getRange(
    `B1:B${LABEL_DEFINITIONS.length}`,
  ).format.columnWidth = 92;
  instructionSheet.getRange(
    `A2:B${LABEL_DEFINITIONS.length}`,
  ).format.rowHeight = 44;
  instructionSheet.freezePanes.freezeRows(1);

  const xlsx = await SpreadsheetFile.exportXlsx(workbook);
  await xlsx.save(xlsxPath);

  const imported = await SpreadsheetFile.importXlsx(
    await FileBlob.load(xlsxPath),
  );
  const inspectDump = `${xlsxPath}.inspect.ndjson`;
  try {
    await fs.rename(
      inspectDump,
      path.join(PREVIEWS, `${item.stem}_inspect.ndjson`),
    );
  } catch (error) {
    if (error?.code !== "ENOENT") {
      throw error;
    }
  }
  const annotation = imported.worksheets.getItem("Annotation");
  const importedValues = annotation.getUsedRange().values;
  const importedHeaders = importedValues[0].map((value) => String(value ?? ""));
  if (
    importedValues.length !== item.rows + 1
    || importedHeaders.length !== item.columns
  ) {
    throw new Error(`${item.stem}: exported workbook shape mismatch`);
  }
  for (let rowIndex = 0; rowIndex < importedValues.length; rowIndex += 1) {
    for (let columnIndex = 0; columnIndex < item.columns; columnIndex += 1) {
      const before = String(values[rowIndex][columnIndex] ?? "");
      const after = String(importedValues[rowIndex][columnIndex] ?? "");
      if (before !== after) {
        throw new Error(
          `${item.stem}: CSV/XLSX mismatch at row ${rowIndex + 1}, `
          + `column ${columnIndex + 1}`,
        );
      }
    }
  }
  for (const columnIndex of labelColumns(importedHeaders)) {
    for (let rowIndex = 1; rowIndex < importedValues.length; rowIndex += 1) {
      if (String(importedValues[rowIndex][columnIndex] ?? "").trim() !== "") {
        throw new Error(`${item.stem}: exported label cells are not blank`);
      }
    }
  }
  const formulaValues = annotation.getUsedRange().formulas;
  let formulaCount = 0;
  for (const row of formulaValues ?? []) {
    for (const formula of row ?? []) {
      if (typeof formula === "string" && formula.startsWith("=")) {
        formulaCount += 1;
      }
    }
  }
  if (formulaCount > 0) {
    throw new Error(`${item.stem}: unexpected formulas detected`);
  }

  const topPreview = await imported.render({
    sheetName: "Annotation",
    range: `A1:${lastColumn}9`,
    scale: 1,
    format: "png",
  });
  const instructionPreview = await imported.render({
    sheetName: "Instructions",
    range: `A1:B${LABEL_DEFINITIONS.length}`,
    scale: 1,
    format: "png",
  });
  const topPreviewPath = path.join(PREVIEWS, `${item.stem}_annotation.png`);
  const instructionPreviewPath = path.join(
    PREVIEWS,
    `${item.stem}_instructions.png`,
  );
  await fs.writeFile(
    topPreviewPath,
    new Uint8Array(await topPreview.arrayBuffer()),
  );
  await fs.writeFile(
    instructionPreviewPath,
    new Uint8Array(await instructionPreview.arrayBuffer()),
  );
  const structure = await imported.inspect({
    kind: "sheet",
    include: "id,name",
    maxChars: 2000,
  });
  return {
    batch_id: `batch_${item.batch}`,
    reviewer: item.reviewer,
    csv_path: path.relative(ROOT, csvPath),
    xlsx_path: path.relative(ROOT, xlsxPath),
    rows: item.rows,
    columns: item.columns,
    sheets: ["Annotation", "Instructions"],
    label_cells_blank: true,
    hidden_sampling_fields_absent: true,
    csv_xlsx_content_identical: true,
    formulas_detected: false,
    annotation_rendered_for_qa: true,
    instructions_rendered_for_qa: true,
    qa_artifacts_retained_in_project: false,
    structure_inspection: structure,
  };
}

await fs.mkdir(PREVIEWS, { recursive: true });
const validation = [];
for (const item of EXPECTED) {
  validation.push(await buildOne(item));
}
const result = {
  phase: "W5-C-A",
  status: "PASS",
  tool: "@oai/artifact-tool",
  workbook_count: validation.length,
  all_label_cells_blank: validation.every((item) => item.label_cells_blank),
  all_hidden_sampling_fields_absent: validation.every(
    (item) => item.hidden_sampling_fields_absent,
  ),
  all_formula_scans_clear: validation.every(
    (item) => !item.formulas_detected,
  ),
  all_csv_xlsx_content_identical: validation.every(
    (item) => item.csv_xlsx_content_identical,
  ),
  workbooks: validation,
};
await fs.writeFile(
  path.join(REPORTS, "workbook_validation.json"),
  JSON.stringify(result, null, 2),
  "utf8",
);
process.stdout.write(
  JSON.stringify({
    status: result.status,
    workbook_count: result.workbook_count,
    output: path.join(REPORTS, "workbook_validation.json"),
  }),
);
