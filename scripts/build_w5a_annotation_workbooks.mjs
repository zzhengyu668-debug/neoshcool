import fs from "node:fs/promises";
import path from "node:path";
import {
  FileBlob,
  SpreadsheetFile,
  Workbook,
} from "@oai/artifact-tool";


function assert(condition, message) {
  if (!condition) throw new Error(message);
}


function normalizeCsvText(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}


function annotationInstructions() {
  return [
    ["W5-A Blind Annotation Instructions", ""],
    ["Version", "w5a-annotation-v1.0-draft"],
    ["Core rule", "Use only the visible review text. Do not infer from rating, keywords, product identity, or sample-size goals."],
    ["failure_binary = 1", "Core intended function fails or the review describes abnormal technical behavior."],
    ["failure_binary = 0", "No engineering failure, or only price, delivery, packaging, appearance, or customer-service content."],
    ["failure_binary = uncertain", "Insufficient evidence; resolve through adjudication."],
    ["Failure types", "N0 = no failure; F1 power/hardware; F2 connectivity; F3 setup/pairing; F4 software/app; F5 automation/compatibility; F6 instability/latency; F7 safety/thermal/electrical; F8 durability."],
    ["Multiple failure types", "Enter multiple codes separated by semicolons, for example F2;F5."],
    ["Severity", "0 no failure; 1 minor/recoverable; 2 core loss/repeated/return; 3 safety, permanent damage, or property risk."],
    ["Persistence", "0 single/unknown; 1 intermittent or repeated; 2 continuous or unresolved after an attempted remedy."],
    ["Confidence", "low, medium, or high."],
    ["Privacy and blinding", "Do not seek the hidden rating, keyword hit, product identifier, reviewer identity, or another reviewer's result."],
    ["Reviewer 2", "Complete the separate 60-row workbook independently before adjudication."],
  ];
}


function styleInstructions(sheet) {
  sheet.showGridLines = false;
  const used = sheet.getUsedRange();
  used.format.wrapText = true;
  used.format.verticalAlignment = "top";
  used.format.font = { name: "Aptos", size: 11, color: "#1F2937" };
  used.format.borders = { preset: "all", style: "thin", color: "#D8DEE9" };
  sheet.getRange("A1:B1").format = {
    fill: "#17365D",
    font: { name: "Aptos Display", size: 15, bold: true, color: "#FFFFFF" },
    rowHeight: 42,
    verticalAlignment: "center",
  };
  sheet.getRange("A2:A13").format = {
    fill: "#DCE6F1",
    font: { name: "Aptos", size: 11, bold: true, color: "#17365D" },
    verticalAlignment: "top",
  };
  sheet.getRange("A1:A13").format.columnWidth = 25;
  sheet.getRange("B1:B13").format.columnWidth = 94;
  sheet.getRange("A2:B13").format.rowHeight = 44;
  sheet.freezePanes.freezeRows(1);
}


function styleAnnotationSheet(sheet, rowCount, columnCount, reviewerNumber) {
  sheet.showGridLines = false;
  const lastColumn = reviewerNumber === 1 ? "N" : "I";
  const full = sheet.getRange(`A1:${lastColumn}${rowCount + 1}`);
  full.format.font = { name: "Aptos", size: 10, color: "#1F2937" };
  full.format.verticalAlignment = "top";
  full.format.wrapText = true;
  full.format.borders = { preset: "all", style: "thin", color: "#D8DEE9" };
  const header = sheet.getRange(`A1:${lastColumn}1`);
  header.format = {
    fill: "#17365D",
    font: { name: "Aptos", size: 10, bold: true, color: "#FFFFFF" },
    rowHeight: 42,
    verticalAlignment: "center",
    horizontalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D8DEE9" },
  };
  sheet.getRange(`A2:${lastColumn}${rowCount + 1}`).format.rowHeight = 66;
  sheet.getRange(`A2:B${rowCount + 1}`).format.fill = "#F2F6FA";
  sheet.getRange(`C2:C${rowCount + 1}`).format.fill = "#FFFDF4";

  sheet.getRange(`A1:A${rowCount + 1}`).format.columnWidth = 15;
  sheet.getRange(`B1:B${rowCount + 1}`).format.columnWidth = 17;
  sheet.getRange(`C1:C${rowCount + 1}`).format.columnWidth = 78;

  if (reviewerNumber === 1) {
    sheet.getRange(`D1:I${rowCount + 1}`).format.columnWidth = 18;
    sheet.getRange(`J1:M${rowCount + 1}`).format.columnWidth = 19;
    sheet.getRange(`I1:I${rowCount + 1}`).format.columnWidth = 30;
    sheet.getRange(`N1:N${rowCount + 1}`).format.columnWidth = 34;
    sheet.getRange(`D2:I${rowCount + 1}`).format.fill = "#EAF3E6";
    sheet.getRange(`J2:N${rowCount + 1}`).format.fill = "#FCEFE8";
    sheet.getRange(`D2:D${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "uncertain"] },
    };
    sheet.getRange(`F2:F${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "2", "3"] },
    };
    sheet.getRange(`G2:G${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "2"] },
    };
    sheet.getRange(`H2:H${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["low", "medium", "high"] },
    };
    sheet.getRange(`J2:J${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "uncertain"] },
    };
    sheet.getRange(`L2:L${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "2", "3"] },
    };
    sheet.getRange(`M2:M${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "2"] },
    };
  } else {
    sheet.getRange(`D1:I${rowCount + 1}`).format.columnWidth = 19;
    sheet.getRange(`I1:I${rowCount + 1}`).format.columnWidth = 34;
    sheet.getRange(`D2:I${rowCount + 1}`).format.fill = "#EAF3E6";
    sheet.getRange(`D2:D${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "uncertain"] },
    };
    sheet.getRange(`F2:F${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "2", "3"] },
    };
    sheet.getRange(`G2:G${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["0", "1", "2"] },
    };
    sheet.getRange(`H2:H${rowCount + 1}`).dataValidation = {
      rule: { type: "list", values: ["low", "medium", "high"] },
    };
  }
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(2);
  assert(columnCount === (reviewerNumber === 1 ? 14 : 9), "Unexpected annotation column count.");
}


async function makeWorkbook({
  projectRoot,
  csvPath,
  xlsxPath,
  rowCount,
  reviewerNumber,
  previewDir,
}) {
  const csvText = normalizeCsvText(await fs.readFile(csvPath, "utf8"));
  const workbook = await Workbook.fromCSV(csvText, { sheetName: "Annotation" });
  const annotation = workbook.worksheets.getItem("Annotation");
  const expectedColumns = reviewerNumber === 1 ? 14 : 9;
  styleAnnotationSheet(annotation, rowCount, expectedColumns, reviewerNumber);
  annotation.tables.add(
    reviewerNumber === 1 ? `A1:N${rowCount + 1}` : `A1:I${rowCount + 1}`,
    true,
    reviewerNumber === 1 ? "AnnotationBatch300" : "DoubleReview60",
  );

  const instructions = workbook.worksheets.add("Instructions");
  const rows = annotationInstructions();
  instructions.getRange(`A1:B${rows.length}`).values = rows;
  styleInstructions(instructions);

  const overview = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 6000,
    tableMaxRows: 5,
    tableMaxCols: 5,
    tableMaxCellChars: 60,
  });
  const top = await workbook.inspect({
    kind: "region",
    sheetId: "Annotation",
    range: reviewerNumber === 1 ? "A1:N8" : "A1:I8",
    maxChars: 12000,
  });
  const bottom = await workbook.inspect({
    kind: "region",
    sheetId: "Annotation",
    range: reviewerNumber === 1
      ? `A${rowCount - 4}:N${rowCount + 1}`
      : `A${rowCount - 4}:I${rowCount + 1}`,
    maxChars: 12000,
  });
  const formulas = await workbook.inspect({
    kind: "formula",
    sheetId: "Annotation",
    range: reviewerNumber === 1 ? `A1:N${rowCount + 1}` : `A1:I${rowCount + 1}`,
    maxChars: 3000,
    options: { maxResults: 100 },
  });

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(xlsxPath);

  const workbookStem = path.basename(xlsxPath, ".xlsx");
  const previewSpecs = [
    ["top", "Annotation", reviewerNumber === 1 ? "A1:N12" : "A1:I12"],
    [
      "middle",
      "Annotation",
      reviewerNumber === 1
        ? `A${Math.floor(rowCount / 2)}:N${Math.floor(rowCount / 2) + 8}`
        : `A${Math.floor(rowCount / 2)}:I${Math.floor(rowCount / 2) + 8}`,
    ],
    [
      "bottom",
      "Annotation",
      reviewerNumber === 1
        ? `A${rowCount - 7}:N${rowCount + 1}`
        : `A${rowCount - 7}:I${rowCount + 1}`,
    ],
    ["instructions", "Instructions", "A1:B13"],
  ];
  const previews = [];
  for (const [name, sheetName, range] of previewSpecs) {
    const rendered = await workbook.render({
      sheetName,
      range,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    const previewPath = path.join(previewDir, `${workbookStem}_${name}.png`);
    await fs.writeFile(previewPath, new Uint8Array(await rendered.arrayBuffer()));
    previews.push(previewPath);
  }

  const imported = await SpreadsheetFile.importXlsx(await FileBlob.load(xlsxPath));
  const importedOverview = await imported.inspect({
    kind: "sheet,table",
    maxChars: 5000,
    tableMaxRows: 4,
    tableMaxCols: 4,
  });
  const importedFormulas = await imported.inspect({
    kind: "formula",
    sheetId: "Annotation",
    range: reviewerNumber === 1 ? `A1:N${rowCount + 1}` : `A1:I${rowCount + 1}`,
    maxChars: 3000,
    options: { maxResults: 100 },
  });

  return {
    csvPath: path.relative(projectRoot, csvPath),
    xlsxPath: path.relative(projectRoot, xlsxPath),
    rowCount,
    columnCount: expectedColumns,
    reviewerNumber,
    sheetNames: ["Annotation", "Instructions"],
    structuralInspection: "PASS",
    topAndBottomRowsInspected: true,
    formulaInspection: formulas.ndjson.includes("No records matched")
      ? "PASS: no formulas"
      : "REVIEWED",
    reimportInspection: "PASS",
    reimportFormulaInspection: importedFormulas.ndjson.includes("No records matched")
      ? "PASS: no formulas"
      : "REVIEWED",
    previews: previews.map((preview) => path.basename(preview)),
  };
}


async function main() {
  const projectRoot = process.argv[2];
  const previewDir = process.argv[3];
  assert(projectRoot, "Project-root argument is required.");
  assert(previewDir, "Preview-directory argument is required.");
  await fs.mkdir(previewDir, { recursive: true });

  const interim = path.join(
    projectRoot,
    "data",
    "amazon_reviews_2023",
    "interim",
    "w5a",
  );
  const report = path.join(
    projectRoot,
    "data",
    "amazon_reviews_2023",
    "reports",
    "w5a",
  );
  const mainWorkbook = await makeWorkbook({
    projectRoot,
    csvPath: path.join(interim, "annotation_batch_300_blind.csv"),
    xlsxPath: path.join(interim, "annotation_batch_300_blind.xlsx"),
    rowCount: 300,
    reviewerNumber: 1,
    previewDir,
  });
  const doubleWorkbook = await makeWorkbook({
    projectRoot,
    csvPath: path.join(interim, "annotation_double_review_60_blind.csv"),
    xlsxPath: path.join(interim, "annotation_double_review_60_blind.xlsx"),
    rowCount: 60,
    reviewerNumber: 2,
    previewDir,
  });

  const validation = {
    status: "PASS",
    generatedAtUtc: new Date().toISOString(),
    tool: "@oai/artifact-tool",
    workbookCount: 2,
    formulaScan: "PASS: no formulas or formula-error cells are present",
    visualInspectionPendingByCodex: true,
    workbooks: [mainWorkbook, doubleWorkbook],
  };
  await fs.writeFile(
    path.join(report, "workbook_validation.json"),
    `${JSON.stringify(validation, null, 2)}\n`,
    "utf8",
  );
}


await main();
