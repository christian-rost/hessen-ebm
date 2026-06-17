import React from "react";
import { createRoot } from "react-dom/client";
import { AlertCircle, CheckCircle2, Database, FileJson, FileText, RefreshCw, ShieldCheck, UploadCloud } from "lucide-react";
import varisanoLogo from "./assets/varisano-logo-neutral-landscape-rgb.svg";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

function formatEuro(value) {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" }).format(value);
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
          <img className="brand-logo" src={varisanoLogo} alt="varisano" />
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
                <span>{file ? file.name : "Noch keine Datei gewaehlt"}</span>
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
                <p>PDF-Evidenz, GOP-Kandidaten und Review-Hinweise fuer eine nachvollziehbare Abrechnung</p>
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
    return <div className="brand-status"><span className="status-pill">Katalog wird geprueft</span></div>;
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
  const [busy, setBusy] = React.useState(null);
  const [message, setMessage] = React.useState(null);
  const [uploadResult, setUploadResult] = React.useState(null);

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
      await onRefresh(adminToken);
    } finally {
      setBusy(null);
    }
  }

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
          <p className="admin-copy">
            Lade eine vorbereitete `ebm_kbv.sqlite` hoch. Der aktive Katalog wird validiert, gesichert und atomar ersetzt.
          </p>

          <label className="field-label" htmlFor="admin-token">Admin Token</label>
          <input
            id="admin-token"
            className="text-input"
            type="password"
            value={adminToken}
            placeholder="Nur bei gesetztem ADMIN_TOKEN"
            onChange={(event) => rememberToken(event.target.value)}
          />

          <div className="drop-zone compact">
            <Database size={24} />
            <label htmlFor="catalog-upload">SQLite auswaehlen</label>
            <input
              id="catalog-upload"
              type="file"
              accept=".sqlite,.db,application/octet-stream"
              onChange={(event) => setCatalogFile(event.target.files?.[0] || null)}
            />
            <span>{catalogFile ? catalogFile.name : "Keine Datei gewaehlt"}</span>
          </div>

          <div className="button-row stacked">
            <button className="btn-secondary btn-block" type="button" disabled={!catalogFile || busy} onClick={() => sendCatalog("validate")}>
              {busy === "validate" ? "Pruefe..." : "Nur validieren"}
            </button>
            <button className="btn-primary btn-block" type="button" disabled={!catalogFile || busy} onClick={() => sendCatalog("upload")}>
              {busy === "upload" ? "Importiere..." : "Einspielen / ersetzen"}
            </button>
          </div>

          {message && (
            <div className={`message ${message.includes("fehlgeschlagen") || message.includes("invalid") ? "error" : "success"}`}>
              {message.includes("fehlgeschlagen") || message.includes("invalid") ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
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
          {uploadResult?.import?.backup_path && (
            <div className="backup-note">
              Backup angelegt: <code>{uploadResult.import.backup_path}</code>
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
                <td>{item.catalog_source}</td>
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

      <DetailList title="Nicht uebernommen" items={result.excluded_evidence.map((item, index) => ({
        key: `excluded-${index}`,
        title: item.evidence,
        detail: item.reason
      }))} />
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
        <p className="muted-text">Keine Eintraege.</p>
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
