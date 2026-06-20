import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Cpu,
  Database,
  Download,
  ExternalLink,
  FileSearch,
  FileText,
  FolderSync,
  Gauge,
  Loader2,
  RefreshCw,
  Search,
} from 'lucide-react';
import './styles.css';

const API = '/api';
const LOCAL_OPEN_HELPER = 'http://127.0.0.1:8765/open';

function App() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [selected, setSelected] = useState(null);
  const [stats, setStats] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [events, setEvents] = useState([]);
  const [categories, setCategories] = useState([{ path: '', label: '全部资料' }]);
  const [scope, setScope] = useState('');
  const [citationFor, setCitationFor] = useState(null);
  const [textContext, setTextContext] = useState(null);
  const [searching, setSearching] = useState(false);
  const [scanLoading, setScanLoading] = useState(false);
  const [openStatus, setOpenStatus] = useState(null);

  useEffect(() => {
    refreshStatus();
    const timer = window.setInterval(refreshStatus, 4000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      setSelected(null);
      return;
    }
    setSearching(true);
    const timer = window.setTimeout(async () => {
      try {
        const data = await getJson(
          `${API}/search?q=${encodeURIComponent(query)}&scope=${encodeURIComponent(scope)}&limit=80`,
        );
        setResults(data.results || []);
        setSelected(data.results?.[0] || null);
        setCitationFor(null);
      } finally {
        setSearching(false);
      }
    }, 240);
    return () => window.clearTimeout(timer);
  }, [query, scope]);

  useEffect(() => {
    if (!selected) {
      setTextContext(null);
      return;
    }
    getJson(`${API}/documents/${selected.document_id}/text?match_id=${selected.match_id}`)
      .then(setTextContext)
      .catch(() => setTextContext(null));
  }, [selected]);

  useEffect(() => {
    setOpenStatus(null);
  }, [selected?.document_id]);

  async function refreshStatus() {
    const [statsData, jobsData, eventsData, categoriesData] = await Promise.all([
      getJson(`${API}/stats`).catch(() => null),
      getJson(`${API}/jobs`).catch(() => ({ jobs: [] })),
      getJson(`${API}/events`).catch(() => ({ events: [] })),
      getJson(`${API}/categories`).catch(() => ({ categories: [{ path: '', label: '全部资料' }] })),
    ]);
    if (statsData) setStats(statsData);
    setJobs(jobsData.jobs || []);
    setEvents(eventsData.events || []);
    setCategories(categoriesData.categories || [{ path: '', label: '全部资料' }]);
  }

  async function triggerScan() {
    setScanLoading(true);
    try {
      await postJson(`${API}/scan`);
      await refreshStatus();
    } finally {
      setScanLoading(false);
    }
  }

  async function openOriginalFile() {
    if (!selected) return;
    setOpenStatus({ tone: 'info', message: '正在打开...' });
    try {
      const data = await openLocalDocument(selected);
      setOpenStatus({ tone: 'ok', message: openSuccessMessage(data) });
      window.setTimeout(() => setOpenStatus(null), 2200);
    } catch (error) {
      setOpenStatus({ tone: 'error', message: error.message || '无法打开本地文件' });
    }
  }

  const activeJob = stats?.latest_job;
  const selectedPdfUrl = useMemo(() => {
    if (!selected) return '';
    const canUsePdf = selected.ext === '.pdf' || selected.searchable_pdf;
    if (!canUsePdf) return '';
    const page = selected.page ? `#page=${selected.page}&search=${encodeURIComponent(query)}` : '';
    return `${API}/files/${selected.document_id}/pdf${page}`;
  }, [selected, query]);

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <FileSearch size={20} />
          </div>
          <div>
            <h1>本地资料检索</h1>
            <p>PP-OCRv6 API · 本机索引</p>
          </div>
        </div>

        <section className="status-stack">
          <Metric icon={<Database size={18} />} label="已索引" value={stats?.documents?.ready ?? 0} />
          <Metric icon={<FolderSync size={18} />} label="待处理" value={stats?.jobs?.queued ?? 0} tone="amber" />
          <Metric icon={<Cpu size={18} />} label="OCR 设备" value={ocrDeviceLabel(stats)} tone={isGpuReady(stats) ? 'blue' : 'green'} />
          <Metric icon={<Gauge size={18} />} label="资源策略" value={resourceLimitLabel(stats)} tone="blue" />
        </section>

        <button className="scan-button" type="button" onClick={triggerScan} disabled={scanLoading}>
          {scanLoading ? <Loader2 className="spin" size={17} /> : <RefreshCw size={17} />}
          重新扫描
        </button>

        <section className="queue">
          <div className="section-title">
            <Clock3 size={16} />
            处理队列
          </div>
          {activeJob ? (
            <div className="active-job">
              <div className="job-top">
                <span>{activeJob.message || '正在处理'}</span>
                <b>{Math.round((activeJob.progress || 0) * 100)}%</b>
              </div>
              <div className="progress-track">
                <span style={{ width: `${Math.max(4, Math.round((activeJob.progress || 0) * 100))}%` }} />
              </div>
            </div>
          ) : (
            <p className="muted">暂无运行任务</p>
          )}
          <div className="job-list">
            {jobs.slice(0, 7).map((job) => (
              <div className={`job-row ${job.status}`} key={job.id}>
                {jobIcon(job.status)}
                <span title={jobTooltip(job)}>{jobLabel(job)}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="events">
          <div className="section-title">
            <FileText size={16} />
            最近变化
          </div>
          {events.slice(0, 8).map((event) => (
            <div className="event-row" key={event.id} title={event.message}>
              <span className={`event-dot ${event.type}`} />
              <span>{event.message}</span>
            </div>
          ))}
        </section>
      </aside>

      <section className="search-pane">
        <header className="topbar">
          <div className="search-box">
            <Search size={20} />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索资料内容..."
              autoFocus
            />
            {searching && <Loader2 className="spin" size={18} />}
          </div>
          <label className="scope-select">
            <span>范围</span>
            <select value={scope} onChange={(event) => setScope(event.target.value)}>
              {categories.map((category) => (
                <option value={category.path} key={category.path || 'all'}>
                  {category.label}
                </option>
              ))}
            </select>
          </label>
          <div className="top-stats">
            <span>{stats?.documents?.total ?? 0} 份资料</span>
            <span>{formatNumber(stats?.documents?.text_chars ?? 0)} 字</span>
          </div>
        </header>

        <div className="result-summary">
          <b>{query.trim() ? `${results.length} 条结果` : '输入关键词开始检索'}</b>
          <span>{scope ? `范围：${scope}` : stats?.fts_tokenizer === 'trigram' ? '中文连续匹配' : '全文索引'}</span>
        </div>

        <div className="results-list">
          {results.map((result) => (
            <article
              role="listitem"
              tabIndex={0}
              className={`result-row ${selected?.match_id === result.match_id ? 'selected' : ''}`}
              key={result.match_id}
              onClick={() => setSelected(result)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') setSelected(result);
              }}
            >
              <div className="file-icon">{result.ext === '.pdf' ? 'PDF' : result.ext.replace('.', '').toUpperCase()}</div>
              <div className="result-body">
                <div className="result-title">
                  <span>{result.title}</span>
                  <small>{result.page ? `PDF 第 ${result.page} 页` : result.line ? `第 ${result.line} 行` : '文本段落'}</small>
                </div>
                <p>{renderSnippet(result.snippet, query)}</p>
                <div className="result-footer">
                  <span className="result-path">{result.rel_path}</span>
                  {result.citation && (
                    <button
                      type="button"
                      className="citation-button"
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelected(result);
                        setCitationFor(citationFor === result.match_id ? null : result.match_id);
                        navigator.clipboard?.writeText(result.citation).catch(() => {});
                      }}
                    >
                      导出引用
                    </button>
                  )}
                </div>
                {citationFor === result.match_id && result.citation && (
                  <div className="citation-box">{result.citation}</div>
                )}
              </div>
            </article>
          ))}
          {query.trim() && !searching && results.length === 0 && (
            <div className="empty-state">
              <FileSearch size={24} />
              <span>没有命中结果</span>
            </div>
          )}
        </div>
      </section>

      <aside className="preview-pane">
        {selected ? (
          <>
            <div className="preview-header">
              <div>
                <h2>{selected.title}</h2>
                <p>{selected.page ? `PDF 第 ${selected.page} 页` : selected.line ? `第 ${selected.line} 行` : selected.rel_path}</p>
                {openStatus && <p className={`open-status ${openStatus.tone}`}>{openStatus.message}</p>}
              </div>
              <div className="preview-actions">
                <button type="button" onClick={openOriginalFile} title="打开本地文件">
                  <ExternalLink size={17} />
                </button>
                {(selected.ext === '.pdf' || selected.searchable_pdf) && (
                  <a href={`${API}/files/${selected.document_id}/pdf`} target="_blank" rel="noreferrer" title="查看 OCR 版">
                    <Download size={17} />
                  </a>
                )}
              </div>
            </div>

            {selectedPdfUrl ? (
              <iframe className="pdf-frame" title="PDF 预览" src={selectedPdfUrl} />
            ) : (
              <TextPreview context={textContext} query={query} />
            )}

            {selectedPdfUrl && <TextPreview context={textContext} query={query} compact />}
          </>
        ) : (
          <div className="preview-empty">
            <FileText size={32} />
            <span>选择一条结果</span>
          </div>
        )}
      </aside>
    </main>
  );
}

function Metric({ icon, label, value, tone = 'neutral' }) {
  return (
    <div className={`metric ${tone}`}>
      {icon}
      <div>
        <span>{label}</span>
        <b>{value}</b>
      </div>
    </div>
  );
}

function TextPreview({ context, query, compact = false }) {
  if (!context) {
    return <div className={compact ? 'text-preview compact' : 'text-preview'} />;
  }
  return (
    <div className={compact ? 'text-preview compact' : 'text-preview'}>
      {context.chunks?.map((chunk) => (
        <p className={chunk.id === context.match_id ? 'hit-line' : ''} key={chunk.id}>
          <span>{chunk.page ? `P${chunk.page}` : chunk.line || chunk.ordinal + 1}</span>
          {highlightPlain(chunk.text, query)}
        </p>
      ))}
    </div>
  );
}

function renderSnippet(snippet, query) {
  const markerParts = String(snippet || '').split(/(<mark>|<\/mark>)/g);
  if (markerParts.length > 1) {
    let marked = false;
    return markerParts.map((part, index) => {
      if (part === '<mark>') {
        marked = true;
        return null;
      }
      if (part === '</mark>') {
        marked = false;
        return null;
      }
      return marked ? <mark key={index}>{part}</mark> : <React.Fragment key={index}>{part}</React.Fragment>;
    });
  }
  return highlightPlain(snippet, query);
}

function highlightPlain(text, query) {
  const value = String(text || '');
  const q = query.trim();
  if (!q) return value;
  const lower = value.toLocaleLowerCase();
  const needle = q.toLocaleLowerCase();
  const index = lower.indexOf(needle);
  if (index < 0) return value;
  return (
    <>
      {value.slice(0, index)}
      <mark>{value.slice(index, index + q.length)}</mark>
      {value.slice(index + q.length)}
    </>
  );
}

function jobIcon(status) {
  if (status === 'done') return <CheckCircle2 size={15} className="ok" />;
  if (status === 'failed') return <AlertTriangle size={15} className="bad" />;
  if (status === 'processing') return <Loader2 size={15} className="spin blue-icon" />;
  return <Clock3 size={15} className="wait" />;
}

function formatNumber(value) {
  return new Intl.NumberFormat('zh-CN', { notation: 'compact' }).format(value || 0);
}

function resourceLimitLabel(stats) {
  const limits = stats?.resources?.limits;
  if (!limits) return '自动';
  return `${limits.max_file_mb}MB`;
}

function jobLabel(job) {
  const title = job.title || job.rel_path;
  if (job.status === 'failed' && job.error) return `${title}: ${job.error}`;
  return title;
}

function jobTooltip(job) {
  return job.error || job.message || job.rel_path;
}

function ocrDeviceLabel(stats) {
  if (stats?.ocr?.engine === 'api' || stats?.ocr?.actual_device === 'api') {
    return 'API';
  }
  const actual = stats?.ocr?.actual_device;
  if (actual?.startsWith('gpu')) return 'GPU';
  if (actual === 'cpu') return 'CPU';
  if (stats?.ocr?.cuda_available) return 'GPU 可用';
  return 'CPU';
}

function isGpuReady(stats) {
  if (stats?.ocr?.engine === 'api' || stats?.ocr?.actual_device === 'api') {
    return false;
  }
  return Boolean(stats?.ocr?.actual_device?.startsWith('gpu') || stats?.ocr?.cuda_available);
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} failed`);
  return response.json();
}

async function postJson(url) {
  const response = await fetch(url, { method: 'POST' });
  if (!response.ok) throw new Error(`POST ${url} failed`);
  return response.json();
}

async function openLocalDocument(result) {
  try {
    const response = await fetch(LOCAL_OPEN_HELPER, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rel_path: result.rel_path }),
    });
    if (!response.ok) {
      const error = new Error(await responseErrorMessage(response, '本地打开助手返回错误'));
      error.fromOpenHelper = true;
      throw error;
    }
    return response.json().catch(() => ({ ok: true }));
  } catch (error) {
    if (error.fromOpenHelper) throw error;
  }

  try {
    const response = await fetch(`${API}/files/${result.document_id}/open`, { method: 'POST' });
    if (response.ok) return response.json().catch(() => ({ ok: true }));
    throw new Error(await responseErrorMessage(response, '后端无法打开本地文件'));
  } catch (error) {
    throw new Error('无法打开本地文件，请运行 scripts\\start.ps1 启动本地打开助手');
  }
}

function openSuccessMessage(data) {
  if (data?.method === 'notepad') return '已调用 Notepad 打开';
  if (data?.method === 'powershell-start-process') return '已请求系统打开';
  return '已请求打开本地文件';
}

async function responseErrorMessage(response, fallback) {
  const data = await response.json().catch(() => null);
  return data?.detail || data?.error || fallback;
}

createRoot(document.getElementById('root')).render(<App />);
