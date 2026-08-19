import React from "react";
import { createRoot } from "react-dom/client";
import { AlertCircle, CheckCircle2, Database, FileJson, FileText, Globe2, RefreshCw, ShieldCheck, UploadCloud } from "lucide-react";
import lenusLogo from "./assets/logo_lenus.svg";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

function formatEuro(value) {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" }).format(value);
}

function jobStatusLabel(status) {
  if (status === "queued") return "wartet";
  if (status === "running") return "läuft";
  if (status === "succeeded") return "abgeschlossen";
  if (status === "failed") return "fehlgeschlagen";
  return status || "unbekannt";
}

function App() {
  const [catalog, setCatalog] = React.useState(null);
  const [view, setView] = React.useState("analysis");
  const [file, setFile] = React.useState(null);
  const [result, setResult] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState(null);

  async function refreshCatalog(adminToken = "") {
    const headers = adminToken ? { "X-Admin-Token": adminToken } : {};
    try {
      const adminResponse = await fetch(`${API_BASE}/api/admin/catalog/status`, { headers });
      if (adminResponse.ok) {
        const payload = await adminResponse.json();
        setCatalog(payload);
        return payload;
      }
    } catch {
      // Public fallback below.
    }

    try {
      const response = await fetch(`${API_BASE}/api/catalog/status`);
      const payload = await response.json();
      setCatalog(payload);
      return payload;
    } catch {
      const fallback = { available: false, snapshots: [], regional_catalogs: [] };
      setCatalog(fallback);
      return fallback;
    }
  }

  React.useEffect(() => {
    refreshCatalog();
  }, []);

  async function analyze(event) {
    event.preventDefault();
    if (!file) return;
    setLoading(true);
    setError(null);
    setResult(null);
    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await fetch(`${API_BASE}/api/documents/analyze`, {
        method: "POST",
        body: formData
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `Analyse fehlgeschlagen (${response.status})`);
      }
      setResult(await response.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function downloadJson() {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `rechnung-${result.analysis_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="app-shell">
      <header className="brand-header">
        <div className="brand-lockup">
          <img className="brand-logo" src={lenusLogo} alt="Lenus" />
          <div className="brand-divider" aria-hidden="true" />
          <div className="brand-app-name">EBM-/Hessen-GOP-Abrechnung</div>
        </div>
        <nav className="brand-nav" aria-label="Arbeitsbereich">
          <button className={`brand-nav-btn ${view === "analysis" ? "active" : ""}`} onClick={() => setView("analysis")}>Analyse</button>
          <button className={`brand-nav-btn ${view === "admin" ? "active" : ""}`} onClick={() => setView("admin")}>Admin</button>
        </nav>
        <CatalogStatus catalog={catalog} />
      </header>

      {view === "analysis" ? (
        <section className="workspace">
          <aside className="doc-sidebar">
            <div className="sidebar-head">
              <h2>Dokument</h2>
            </div>
            <form className="upload-panel" onSubmit={analyze}>
              <div className="drop-zone">
                <UploadCloud size={26} />
                <label htmlFor="pdf-upload">Klinisches PDF hochladen</label>
                <input
                  id="pdf-upload"
                  type="file"
                  accept="application/pdf"
                  onChange={(event) => setFile(event.target.files?.[0] || null)}
                />
                <span>{file ? file.name : "Noch keine Datei gewählt"}</span>
              </div>
              <button className="btn-primary btn-block" type="submit" disabled={!file || loading}>
                {loading ? "Analysiere..." : "Rechnung erzeugen"}
              </button>
              {error && (
                <div className="message error">
                  <AlertCircle size={18} />
                  {error}
                </div>
              )}
            </form>
          </aside>
          <section className="doc-detail">
            <div className="doc-detail-header">
              <div className="doc-detail-title">
                <h2>Rechnungsentwurf</h2>
                <p>PDF-Evidenz, GOP-Kandidaten und Review-Hinweise für eine nachvollziehbare Abrechnung</p>
              </div>
              {result && (
                <button className="btn-secondary" onClick={downloadJson}>
                  <FileJson size={16} />
                  JSON Export
                </button>
              )}
            </div>
            <ResultPanel result={result} onDownload={downloadJson} />
          </section>
        </section>
      ) : (
        <AdminPanel catalog={catalog} onCatalogUpdated={setCatalog} onRefresh={refreshCatalog} />
      )}
    </main>
  );
}

function CatalogStatus({ catalog }) {
  if (!catalog) {
    return <div className="brand-status"><span className="status-pill">Katalog wird geprüft</span></div>;
  }
  return (
    <div className="brand-status">
      <div>
        <span className="status-title">{catalog.available ? "Katalog verbunden" : "Katalog fehlt"}</span>
        <span className="status-pill">{catalog.available ? `${catalog.snapshots?.length || 0} EBM / ${catalog.regional_catalogs?.length || 0} regional` : "Admin"}</span>
      </div>
    </div>
  );
}

function AdminPanel({ catalog, onCatalogUpdated, onRefresh }) {
  const [adminToken, setAdminToken] = React.useState(localStorage.getItem("hessen-ebm-admin-token") || "");
  const [catalogFile, setCatalogFile] = React.useState(null);
  const [regionalFile, setRegionalFile] = React.useState(null);
  const [regionalQuarter, setRegionalQuarter] = React.useState("2026/Q3");
  const [regionalRegion, setRegionalRegion] = React.useState("Hessen");
  const [regionalSource, setRegionalSource] = React.useState("KV_HESSEN_GOP");
  const [regionalCatalogId, setRegionalCatalogId] = React.useState("");
  const [regionalReplace, setRegionalReplace] = React.useState(true);
  const [ebmQuarter, setEbmQuarter] = React.useState("2026/Q1");
  const [ebmReplace, setEbmReplace] = React.useState(true);
  const [busy, setBusy] = React.useState(null);
  const [message, setMessage] = React.useState(null);
  const [uploadResult, setUploadResult] = React.useState(null);
  const [scrapeJob, setScrapeJob] = React.useState(null);

  function tokenHeaders() {
    return adminToken ? { "X-Admin-Token": adminToken } : {};
  }

  function rememberToken(value) {
    setAdminToken(value);
    if (value) {
      localStorage.setItem("hessen-ebm-admin-token", value);
    } else {
      localStorage.removeItem("hessen-ebm-admin-token");
    }
  }

  async function refresh() {
    setBusy("refresh");
    setMessage(null);
    try {
      const payload = await onRefresh(adminToken);
      if (payload?.active_job?.kind === "ebm_scrape") {
        setScrapeJob(payload.active_job);
        setBusy("ebm-scrape");
        setMessage(payload.active_job.message);
      }
    } finally {
      setBusy((current) => (current === "refresh" ? null : current));
    }
  }

  React.useEffect(() => {
    if (catalog?.active_job?.kind === "ebm_scrape") {
      setScrapeJob(catalog.active_job);
      setBusy("ebm-scrape");
      setMessage(catalog.active_job.message);
    }
  }, [catalog?.active_job?.id]);

  React.useEffect(() => {
    if (!scrapeJob || ["succeeded", "failed"].includes(scrapeJob.status)) {
      return undefined;
    }

    let cancelled = false;
    let intervalId = null;

    async function pollJob() {
      try {
        const response = await fetch(`${API_BASE}/api/admin/catalog/jobs/${scrapeJob.id}`, {
          headers: tokenHeaders()
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || `Job-Status konnte nicht geladen werden (${response.status})`);
        }
        if (cancelled) return;

        const job = payload.job;
        setScrapeJob(job);
        if (payload.status) {
          onCatalogUpdated(payload.status);
        }
        if (job.status === "succeeded") {
          if (intervalId) window.clearInterval(intervalId);
          setBusy(null);
          setUploadResult(job.result);
          const details = job.result?.import?.snapshot?.detail_count ?? "?";
          const quarter = job.params?.quarter || ebmQuarter.trim();
          setMessage(`KBV-EBM ${quarter} wurde importiert (${details} Details).`);
        } else if (job.status === "failed") {
          if (intervalId) window.clearInterval(intervalId);
          setBusy(null);
          setUploadResult(null);
          setMessage(job.error || job.message || "EBM-Scraping fehlgeschlagen.");
        } else {
          setBusy("ebm-scrape");
          setMessage(job.message || `KBV-EBM ${job.params?.quarter || ""} wird importiert.`);
        }
      } catch (err) {
        if (cancelled) return;
        setMessage(err.message);
      }
    }

    pollJob();
    intervalId = window.setInterval(pollJob, 4000);
    return () => {
      cancelled = true;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [scrapeJob?.id, adminToken]);

  async function sendCatalog(endpoint) {
    if (!catalogFile) return;
    setBusy(endpoint);
    setMessage(null);
    setUploadResult(null);
    const formData = new FormData();
    formData.append("file", catalogFile);
    try {
      const response = await fetch(`${API_BASE}/api/admin/catalog/${endpoint}`, {
        method: "POST",
        headers: tokenHeaders(),
        body: formData
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `Admin-Aktion fehlgeschlagen (${response.status})`);
      }
      setUploadResult(payload);
      if (payload.status) {
        onCatalogUpdated(payload.status);
      }
      setMessage(endpoint === "upload" ? "Katalog wurde eingespielt." : "Katalogdatei ist valide.");
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(null);
    }
  }

  async function importRegionalCatalog() {
    if (!regionalFile || !regionalQuarter.trim()) return;
    setBusy("regional-import");
    setMessage(null);
    setUploadResult(null);
    const formData = new FormData();
    formData.append("file", regionalFile);
    formData.append("quarter", regionalQuarter.trim());
    formData.append("region", regionalRegion.trim() || "Hessen");
    formData.append("source_system", regionalSource.trim() || "KV_HESSEN_GOP");
    formData.append("catalog_id", regionalCatalogId.trim());
    formData.append("replace", String(regionalReplace));
    try {
      const response = await fetch(`${API_BASE}/api/admin/catalog/regional/import`, {
        method: "POST",
        headers: tokenHeaders(),
        body: formData
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `Regionalimport fehlgeschlagen (${response.status})`);
      }
      setUploadResult(payload);
      if (payload.status) {
        onCatalogUpdated(payload.status);
      }
      const count = payload.import?.result?.regional_gops ?? "?";
      setMessage(`Regionaler Katalog wurde importiert (${count} GOP-Einträge).`);
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(null);
    }
  }

  async function scrapeEbmCatalog() {
    if (!ebmQuarter.trim()) return;
    setBusy("ebm-scrape");
    setMessage(null);
    setUploadResult(null);
    setScrapeJob(null);
    const formData = new FormData();
    formData.append("quarter", ebmQuarter.trim());
    formData.append("replace_quarter", String(ebmReplace));
    formData.append("delay", "0.02");
    formData.append("timeout", "30");
    try {
      const response = await fetch(`${API_BASE}/api/admin/catalog/ebm/scrape`, {
        method: "POST",
        headers: tokenHeaders(),
        body: formData
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `EBM-Scraping fehlgeschlagen (${response.status})`);
      }
      if (payload.status) {
        onCatalogUpdated(payload.status);
      }
      if (payload.job) {
        setScrapeJob(payload.job);
        setMessage(`KBV-EBM ${ebmQuarter.trim()} wurde als Hintergrundjob gestartet.`);
      } else {
        setUploadResult(payload);
        setBusy(null);
        const details = payload.import?.snapshot?.detail_count ?? "?";
        setMessage(`KBV-EBM ${ebmQuarter.trim()} wurde importiert (${details} Details).`);
      }
    } catch (err) {
      setMessage(err.message);
      setBusy(null);
    } finally {
      // Background jobs keep the EBM button disabled until polling reaches a terminal state.
    }
  }

  const backupPath = uploadResult?.import?.backup_path || uploadResult?.import?.install?.backup_path;
  const messageIsError = message && (
    message.includes("fehlgeschlagen") ||
    message.includes("failed") ||
    message.includes("invalid") ||
    message.includes("nicht")
  );

  return (
    <section className="workspace">
      <aside className="doc-sidebar">
        <div className="sidebar-head">
          <h2>Administration</h2>
          <button className="icon-btn" type="button" disabled={busy === "refresh"} onClick={refresh} title="Status aktualisieren">
            <RefreshCw size={17} />
          </button>
        </div>
        <section className="upload-panel">
          <div className="admin-side-title">
            <ShieldCheck size={18} />
            <strong>Katalogverwaltung</strong>
          </div>

          <label className="field-label" htmlFor="admin-token">Admin Token</label>
          <input
            id="admin-token"
            className="text-input"
            type="password"
            value={adminToken}
            placeholder="Nur bei gesetztem ADMIN_TOKEN"
            onChange={(event) => rememberToken(event.target.value)}
          />

          <section className="admin-section">
            <div className="admin-section-title">
              <Database size={18} />
              <strong>Vollständige SQLite ersetzen</strong>
            </div>
            <p className="admin-copy">
              Importiert eine vorbereitete komplette <code>ebm_kbv.sqlite</code>. Der aktive Katalog wird validiert, gesichert und atomar ersetzt.
            </p>
            <div className="drop-zone compact">
              <Database size={24} />
              <label htmlFor="catalog-upload">SQLite auswählen</label>
              <input
                id="catalog-upload"
                type="file"
                accept=".sqlite,.db,application/octet-stream"
                onChange={(event) => setCatalogFile(event.target.files?.[0] || null)}
              />
              <span>{catalogFile ? catalogFile.name : "Keine Datei gewählt"}</span>
            </div>

            <div className="button-row stacked">
              <button className="btn-secondary btn-block" type="button" disabled={!catalogFile || busy} onClick={() => sendCatalog("validate")}>
                {busy === "validate" ? "Prüfe..." : "Nur validieren"}
              </button>
              <button className="btn-primary btn-block" type="button" disabled={!catalogFile || busy} onClick={() => sendCatalog("upload")}>
                {busy === "upload" ? "Importiere..." : "Einspielen / ersetzen"}
              </button>
            </div>
          </section>

          <section className="admin-section">
            <div className="admin-section-title">
              <FileText size={18} />
              <strong>Regionalen Katalog importieren</strong>
            </div>
            <p className="admin-copy">
              Importiert ein Hessen-GOP-PDF in die Regionaltabellen der aktiven Datenbank. Vor der Übernahme wird ein Backup erstellt.
            </p>
            <div className="drop-zone compact">
              <UploadCloud size={24} />
              <label htmlFor="regional-upload">Regional-PDF auswählen</label>
              <input
                id="regional-upload"
                type="file"
                accept="application/pdf"
                onChange={(event) => setRegionalFile(event.target.files?.[0] || null)}
              />
              <span>{regionalFile ? regionalFile.name : "Keine Datei gewählt"}</span>
            </div>
            <div className="admin-grid">
              <label>
                <span className="field-label">Quartal</span>
                <input className="text-input" value={regionalQuarter} onChange={(event) => setRegionalQuarter(event.target.value)} placeholder="2026/Q3" />
              </label>
              <label>
                <span className="field-label">Region</span>
                <input className="text-input" value={regionalRegion} onChange={(event) => setRegionalRegion(event.target.value)} placeholder="Hessen" />
              </label>
              <label>
                <span className="field-label">Quelle</span>
                <input className="text-input" value={regionalSource} onChange={(event) => setRegionalSource(event.target.value)} placeholder="KV_HESSEN_GOP" />
              </label>
              <label>
                <span className="field-label">Catalog-ID optional</span>
                <input className="text-input" value={regionalCatalogId} onChange={(event) => setRegionalCatalogId(event.target.value)} placeholder="auto" />
              </label>
            </div>
            <label className="checkbox-row">
              <input type="checkbox" checked={regionalReplace} onChange={(event) => setRegionalReplace(event.target.checked)} />
              vorhandenes Quartal/Region ersetzen
            </label>
            <button className="btn-primary btn-block" type="button" disabled={!regionalFile || !regionalQuarter.trim() || busy} onClick={importRegionalCatalog}>
              {busy === "regional-import" ? "Importiere regionalen Katalog..." : "Regional-Katalog übernehmen"}
            </button>
          </section>

          <section className="admin-section">
            <div className="admin-section-title">
              <Globe2 size={18} />
              <strong>KBV-EBM online scrapen</strong>
            </div>
            <p className="admin-copy">
              Ruft ein Quartal von <code>https://ebm.kbv.de/</code> ab und importiert es als EBM-Snapshot in die aktive Datenbank.
            </p>
            <label>
              <span className="field-label">EBM-Quartal</span>
              <input className="text-input" value={ebmQuarter} onChange={(event) => setEbmQuarter(event.target.value)} placeholder="2026/Q1" />
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={ebmReplace} onChange={(event) => setEbmReplace(event.target.checked)} />
              vorhandenes Quartal ersetzen
            </label>
            <button className="btn-primary btn-block" type="button" disabled={!ebmQuarter.trim() || busy} onClick={scrapeEbmCatalog}>
              {busy === "ebm-scrape" ? "Scrape läuft..." : "EBM-Quartal online importieren"}
            </button>
            {scrapeJob && (
              <div className={`job-status ${scrapeJob.status}`}>
                <strong>EBM-Job: {jobStatusLabel(scrapeJob.status)}</strong>
                <span>{scrapeJob.message}</span>
                <span>Job-ID: <code>{scrapeJob.id}</code></span>
              </div>
            )}
          </section>

          {message && (
            <div className={`message ${messageIsError ? "error" : "success"}`}>
              {messageIsError ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
              {message}
            </div>
          )}
        </section>
      </aside>

      <section className="doc-detail">
        <div className="doc-detail-header">
          <div className="doc-detail-title">
            <h2>Aktiver Katalog</h2>
            <p>EBM-Snapshots, regionale GOP-Kataloge und Backups</p>
          </div>
        </div>
        <section className="result-panel">
          <CatalogDetails catalog={catalog} />
          {backupPath && (
            <div className="backup-note">
              Backup angelegt: <code>{backupPath}</code>
            </div>
          )}
        </section>
      </section>
    </section>
  );
}

function CatalogDetails({ catalog }) {
  if (!catalog) {
    return <p className="muted-text">Katalogstatus wird geladen.</p>;
  }
  if (!catalog.available) {
    return <p className="muted-text">Noch keine aktive Katalogdatenbank unter <code>{catalog.db_path}</code>.</p>;
  }
  return (
    <div className="catalog-detail-grid">
      <div>
        <span>Pfad</span>
        <strong>{catalog.db_path}</strong>
      </div>
      <div>
        <span>EBM-Snapshots</span>
        <strong>{catalog.snapshots?.length || 0}</strong>
      </div>
      <div>
        <span>Regionale Kataloge</span>
        <strong>{catalog.regional_catalogs?.length || 0}</strong>
      </div>
      <div>
        <span>Backups</span>
        <strong>{catalog.backups?.length || 0}</strong>
      </div>
      <DetailList title="Snapshots" items={(catalog.snapshots || []).map((snapshot) => ({
        key: snapshot.quarter,
        title: snapshot.quarter,
        detail: `${snapshot.detail_count} Details, Stand ${snapshot.data_stand || "-"}`
      }))} />
      <DetailList title="Regionale Kataloge" items={(catalog.regional_catalogs || []).map((regional) => ({
        key: regional.catalog_id,
        title: `${regional.source_system} ${regional.region} ${regional.quarter}`,
        detail: `${regional.title || "ohne Titel"}, Stand ${regional.data_stand || "-"}`
      }))} />
    </div>
  );
}

function ResultPanel({ result, onDownload }) {
  if (!result) {
    return (
      <section className="empty-state">
        <FileText size={34} />
        <h2>Noch kein Rechnungsentwurf</h2>
        <p>Nach dem Upload erscheinen hier Segmente, Evidenz, GOPs und Review-Hinweise.</p>
      </section>
    );
  }

  const regionalChecks = Array.isArray(result.catalog_context?.regional_catalog_checks)
    ? result.catalog_context.regional_catalog_checks
    : [];

  return (
    <section className="result-panel">
      <div className="summary-row">
        <SummaryBox label="Positionen" value={result.summary.line_count} />
        <SummaryBox label="Punkte" value={result.summary.points_total} />
        <SummaryBox label="Betrag" value={formatEuro(result.summary.amount_total_eur)} />
        <button className="btn-secondary" onClick={onDownload}>
          <FileJson size={18} />
          JSON Export
        </button>
      </div>

      <RegionalCatalogNotes checks={regionalChecks} />

      <div className="section-header">
        <CheckCircle2 size={20} />
        <h2>Sichere Positionen</h2>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>GOP</th>
              <th>Leistung</th>
              <th>Datum</th>
              <th>Quelle</th>
              <th>Herleitung</th>
              <th>Punkte</th>
              <th>EUR</th>
            </tr>
          </thead>
          <tbody>
            {result.items.map((item) => (
              <tr key={`${item.line}-${item.gop_original}`}>
                <td><code>{item.gop_original}</code></td>
                <td>{item.title}</td>
                <td>{item.service_date || "-"}</td>
                <td className="source-cell">
                  <strong>{item.catalog_source_label || item.catalog_source}</strong>
                  {item.catalog_id && <span>{item.catalog_id}</span>}
                  {item.catalog_data_stand && <span>Stand {item.catalog_data_stand}</span>}
                </td>
                <td className="reason-cell">{item.semantic_reason || item.rule_id}</td>
                <td>{item.points ?? "-"}</td>
                <td>{formatEuro(item.amount_eur)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <TwoColumn>
        <DetailList title="Dokumentsegmente" items={result.segments.map((segment) => ({
          key: segment.segment_id,
          title: `${segment.title}: S. ${segment.start_page}-${segment.end_page}`,
          detail: segment.relevant_for_billing ? "abrechnungsrelevant" : "nur Kontext/Review"
        }))} />
        <DetailList title="Review" items={result.review_candidates.map((candidate, index) => ({
          key: `review-${index}`,
          title: candidate.evidence,
          detail: `${candidate.reason} Seiten: ${candidate.evidence_pages.join(", ")}`
        }))} />
      </TwoColumn>

      <DetailList title="Nicht übernommen" items={result.excluded_evidence.map((item, index) => ({
        key: `excluded-${index}`,
        title: item.evidence,
        detail: item.reason
      }))} />
    </section>
  );
}

function RegionalCatalogNotes({ checks }) {
  const visibleChecks = checks.filter((check) => check?.message);
  if (visibleChecks.length === 0) {
    return null;
  }

  return (
    <section className="catalog-notes">
      {visibleChecks.map((check) => {
        const hasMatches = Array.isArray(check.matched_gops) && check.matched_gops.length > 0;
        return (
          <div className={`catalog-note ${check.checked && hasMatches ? "checked" : "missing"}`} key={`${check.region}-${check.quarter}`}>
            <Database size={17} />
            <div>
              <strong>{check.checked ? "Regionalkatalog geprüft" : "Regionalkatalog-Hinweis"}</strong>
              <span>{check.message}</span>
            </div>
          </div>
        );
      })}
    </section>
  );
}

function SummaryBox({ label, value }) {
  return (
    <div className="summary-box">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TwoColumn({ children }) {
  return <div className="two-column">{children}</div>;
}

function DetailList({ title, items }) {
  return (
    <section className="detail-list">
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="muted-text">Keine Einträge.</p>
      ) : (
        <ul>
          {items.map((item) => (
            <li key={item.key}>
              <strong>{item.title}</strong>
              <span>{item.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

createRoot(document.getElementById("root")).render(<App />);
