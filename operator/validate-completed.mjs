import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { execSync } from "node:child_process";

const sql = `SELECT json_group_array(json_object('id', id, 'job_id', job_id, 'evidence_path', evidence_path)) FROM application_runs WHERE status = 'completed';`;
const completed = JSON.parse(execSync(`sqlite3 data/RealJobs.sqlite "${sql}"`, { encoding: "utf8" }).trim() || "[]");

function sha256File(p) {
  return crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex");
}

const results = [];
for (const row of completed) {
  const dir = row.evidence_path;
  const completionPath = path.join(dir, "completion.json");
  const ok = { id: row.id, job_id: row.job_id, dir, hasCompletion: false, valid: false, errors: [] };
  if (!fs.existsSync(completionPath)) { ok.errors.push("missing completion.json"); results.push(ok); continue; }
  ok.hasCompletion = true;
  const completion = JSON.parse(fs.readFileSync(completionPath, "utf8"));
  const required = ["schema_version","finalized_at","submit_action_count","final_audit","screenshot","upload","action_journal"];
  for (const k of required) if (!(k in completion)) ok.errors.push(`missing ${k}`);
  if (completion.submit_action_count < 1) ok.errors.push("submit_action_count < 1");
  for (const [k, ref] of Object.entries({ final_audit: completion.final_audit, screenshot: completion.screenshot, upload: completion.upload })) {
    if (!ref?.artifact) { ok.errors.push(`${k} missing artifact`); continue; }
    const artifactPath = path.join(dir, ref.artifact);
    if (!fs.existsSync(artifactPath)) { ok.errors.push(`${k} artifact missing ${ref.artifact}`); continue; }
    const actualSha = sha256File(artifactPath);
    if (ref.sha256 && ref.sha256 !== actualSha) ok.errors.push(`${k} sha256 mismatch`);
  }
  if (completion.action_journal?.artifact) {
    const p = path.join(dir, completion.action_journal.artifact);
    if (!fs.existsSync(p)) ok.errors.push("action_journal missing");
    else if (completion.action_journal.sha256 && sha256File(p) !== completion.action_journal.sha256) ok.errors.push("action_journal sha256 mismatch");
  }
  if (ok.errors.length === 0) ok.valid = true;
  results.push(ok);
}
console.log(JSON.stringify(results, null, 2));
