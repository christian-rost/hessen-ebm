import React from "react";
import { createRoot } from "react-dom/client";
import { AlertCircle, CheckCircle2, Database, FileJson, FileText, RefreshCw, ShieldCheck, UploadCloud } from "lucide-react";
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
      <section className="topbar">
        <div>
          <p className="eyebrow">Hessen EBM</p>
          <h1>PDF rein, Evidenz raus, Rechnungsentwurf pruefen.</h1>
        </div>
        <CatalogStatus catalog={catalog} />
      </section>

      <nav className="mode-nav" aria-label="Arbeitsbereich">
        <button className={view === "analysis" ? "active" : ""} onClick={() => setView("analysis")}>Analyse</button>
        <button className={view === "admin" ? "active" : ""} onClick={() => setView("admin")}>Admin</button>
      </nav>

      {view === "analysis" ? (
        <section className="workspace">
          <form className="upload-panel" onSubmit={analyze}>
            <div className="drop-zone">
              <UploadCloud size={28} />
              <label htmlFor="pdf-upload">Klinisches PDF hochladen</label>
              <input
                id="pdf-upload"
                type="file"
                accept="application/pdf"
                onChange={(event) => setFile(event.target.files?.[0] || null)}
              />
              <span>{file ? file.name : "Noch keine Datei gewaehlt"}</span>
            </div>
            <button className="primary-button" type="submit" disabled={!file || loading}>
              {loading ? "Analysiere..." : "Rechnung erzeugen"}
            </button>
            {error && (
              <div className="message error">
                <AlertCircle size={18} />
                {error}
              </div>
            )}
          </form>

          <ResultPanel result={result} onDownload={downloadJson} />
        </section>
      ) : (
        <AdminPanel catalog={catalog} onCatalogUpdated={setCatalog} onRefresh={refreshCatalog} />
      )}
    </main>
  );
}

function CatalogStatus({ catalog }) {
  if (!catalog) {
    return <div className="status-chip muted">Katalog wird geprueft</div>;
  }
  return (
    <div className={`catalog-card ${catalog.available ? "ok" : "warn"}`}>
      <Database size={20} />
      <div>
        <strong>{catalog.available ? "Katalog verbunden" : "Katalog fehlt"}</strong>
        <span>
          {catalog.available
            ? `${catalog.snapshots?.length || 0} EBM-Snapshots, ${catalog.regional_catalogs?.length || 0} regionale Kataloge`
            : "CATALOG_DB_PATH pruefen"}
        </span>
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
    <section className="admin-layout">
      <section className="admin-card">
        <div className="section-header">
          <ShieldCheck size={20} />
          <h2>Katalogverwaltung</h2>
        </div>
        <p className="admin-copy">
          Lade hier eine vorbereitete `ebm_kbv.sqlite` hoch. Das Backend prueft die SQLite-Integritaet und die EBM-Basistabellen, legt ein Backup der aktiven Datenbank an und ersetzt sie danach atomar.
        </p>

        <label className="field-label" htmlFor="admin-token">Admin Token</label>
        <input
          id="admin-token"
          className="text-input"
          type="password"
          value={adminToken}
          placeholder="Nur erforderlich, wenn ADMIN_TOKEN gesetzt ist"
          onChange={(event) => rememberToken(event.target.value)}
        />

        <div className="drop-zone compact">
          <Database size={26} />
          <label htmlFor="catalog-upload">EBM-/Hessen-GOP-SQLite auswaehlen</label>
          <input
            id="catalog-upload"
            type="file"
            accept=".sqlite,.db,application/octet-stream"
            onChange={(event) => setCatalogFile(event.target.files?.[0] || null)}
          />
          <span>{catalogFile ? catalogFile.name : "Noch keine Katalogdatenbank gewaehlt"}</span>
        </div>

        <div className="button-row">
          <button className="secondary-button" type="button" disabled={!catalogFile || busy} onClick={() => sendCatalog("validate")}>
            {busy === "validate" ? "Pruefe..." : "Nur validieren"}
          </button>
          <button className="primary-button inline" type="button" disabled={!catalogFile || busy} onClick={() => sendCatalog("upload")}>
            {busy === "upload" ? "Importiere..." : "Einspielen / ersetzen"}
          </button>
          <button className="icon-button" type="button" disabled={busy === "refresh"} onClick={refresh} title="Status aktualisieren">
            <RefreshCw size={18} />
          </button>
        </div>

        {message && (
          <div className={`message ${message.includes("fehlgeschlagen") || message.includes("invalid") ? "error" : "success"}`}>
            {message.includes("fehlgeschlagen") || message.includes("invalid") ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
            {message}
          </div>
        )}
      </section>

      <section className="admin-card">
        <div className="section-header">
          <Database size={20} />
          <h2>Aktiver Katalog</h2>
        </div>
        <CatalogDetails catalog={catalog} />
        {uploadResult?.import?.backup_path && (
          <div className="backup-note">
            Backup angelegt: <code>{uploadResult.import.backup_path}</code>
          </div>
        )}
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
        <button className="secondary-button" onClick={onDownload}>
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
