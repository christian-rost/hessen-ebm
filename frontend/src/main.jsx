import React from "react";
import { createRoot } from "react-dom/client";
import { AlertCircle, CheckCircle2, Database, FileJson, FileText, UploadCloud } from "lucide-react";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

function formatEuro(value) {
  if (value === null || value === undefined) return "-";
  return new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" }).format(value);
}

function App() {
  const [catalog, setCatalog] = React.useState(null);
  const [file, setFile] = React.useState(null);
  const [result, setResult] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    fetch(`${API_BASE}/api/catalog/status`)
      .then((response) => response.json())
      .then(setCatalog)
      .catch(() => setCatalog({ available: false, snapshots: [], regional_catalogs: [] }));
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

