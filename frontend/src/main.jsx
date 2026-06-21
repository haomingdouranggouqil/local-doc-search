import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Cpu,
  Database,
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
const SEARCH_GROUP_PAGE_SIZE = 200;
const DOCUMENT_HIT_PAGE_SIZE = 100;

function App() {
  const [query, setQuery] = useState('');
  const [resultGroups, setResultGroups] = useState([]);
  const [expandedDocuments, setExpandedDocuments] = useState({});
  const [documentHits, setDocumentHits] = useState({});
  const [selected, setSelected] = useState(null);
  const [stats, setStats] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [events, setEvents] = useState([]);
  const [categories, setCategories] = useState([{ path: '', label: '全部资料' }]);
  const [scope, setScope] = useState('');
  const [citationFor, setCitationFor] = useState(null);
  const [textContext, setTextContext] = useState(null);
  const [searching, setSearching] = useState(false);
  const [hasMoreResults, setHasMoreResults] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [scanLoading, setScanLoading] = useState(false);
  const [retryFailedOnScan, setRetryFailedOnScan] = useState(false);
  const [openStatus, setOpenStatus] = useState(null);
  const searchRequestRef = useRef(0);

  useEffect(() => {
    refreshStatus();
    const timer = window.setInterval(refreshStatus, 4000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!query.trim()) {
      searchRequestRef.current += 1;
      setResultGroups([]);
      setExpandedDocuments({});
      setDocumentHits({});
      setSelected(null);
      setHasMoreResults(false);
      setLoadingMore(false);
      return;
    }
    setSearching(true);
    const requestId = searchRequestRef.current + 1;
    searchRequestRef.current = requestId;
    const timer = window.setTimeout(async () => {
      try {
        const data = await fetchSearchGroups(query, scope, 0);
        if (requestId !== searchRequestRef.current) return;
        const nextGroups = data.groups || [];
        setResultGroups(nextGroups);
        setExpandedDocuments({});
        setDocumentHits({});
        setSelected(null);
        setHasMoreResults(Boolean(data.has_more));
        setCitationFor(null);
      } finally {
        if (requestId === searchRequestRef.current) setSearching(false);
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
      await postJson(`${API}/scan?retry_failed=${retryFailedOnScan ? 'true' : 'false'}`);
      await refreshStatus();
    } finally {
      setScanLoading(false);
    }
  }

  async function loadMoreGroups() {
    if (loadingMore || searching || !hasMoreResults || !query.trim()) return;
    setLoadingMore(true);
    const requestId = searchRequestRef.current;
    const offset = resultGroups.length;
    try {
      const data = await fetchSearchGroups(query, scope, offset);
      if (requestId !== searchRequestRef.current) return;
      const nextGroups = data.groups || [];
      setResultGroups((current) => {
        const seen = new Set(current.map((item) => item.document_id));
        return [...current, ...nextGroups.filter((item) => !seen.has(item.document_id))];
      });
      setHasMoreResults(Boolean(data.has_more));
    } finally {
      if (requestId === searchRequestRef.current) setLoadingMore(false);
    }
  }

  async function toggleDocumentGroup(group) {
    const documentId = group.document_id;
    const wasExpanded = Boolean(expandedDocuments[documentId]);
    setExpandedDocuments((current) => ({
      ...current,
      [documentId]: !current[documentId],
    }));
    if (!wasExpanded && !documentHits[documentId]?.results?.length && !documentHits[documentId]?.loading) {
      await loadDocumentHits(group, 0);
    }
  }

  async function loadDocumentHits(group, offset = 0) {
    const documentId = group.document_id;
    const requestId = searchRequestRef.current;
    setDocumentHits((current) => ({
      ...current,
      [documentId]: {
        results: current[documentId]?.results || [],
        hasMore: Boolean(current[documentId]?.hasMore),
        nextOffset: current[documentId]?.nextOffset || 0,
        loading: true,
        error: null,
      },
    }));
    try {
      const data = await fetchDocumentSearchResults(query, documentId, offset);
      if (requestId !== searchRequestRef.current) return;
      const nextResults = data.results || [];
      setDocumentHits((current) => {
        const previousResults = offset > 0 ? current[documentId]?.results || [] : [];
        const seen = new Set(previousResults.map((item) => item.match_id));
        const results = [
          ...previousResults,
          ...nextResults.filter((item) => !seen.has(item.match_id)),
        ];
        return {
          ...current,
          [documentId]: {
            results,
            hasMore: Boolean(data.has_more),
            nextOffset: data.next_offset ?? results.length,
            loading: false,
            error: null,
          },
        };
      });
    } catch (error) {
      if (requestId !== searchRequestRef.current) return;
      setDocumentHits((current) => ({
        ...current,
        [documentId]: {
          results: current[documentId]?.results || [],
          hasMore: Boolean(current[documentId]?.hasMore),
          nextOffset: current[documentId]?.nextOffset || 0,
          loading: false,
          error: error.message || '无法加载文件内结果',
        },
      }));
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
  const selectedPdfPageImageUrl = useMemo(() => {
    if (!selected || !(selected.ext === '.pdf' || selected.searchable_pdf) || !selected.page) return '';
    const params = new URLSearchParams({ page: String(selected.page) });
    if (selected.match_id) params.set('match_id', String(selected.match_id));
    if (query.trim()) params.set('q', query.trim());
    return `${API}/files/${selected.document_id}/page-image?${params.toString()}`;
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

        <div className="scan-controls">
          <button className="scan-button" type="button" onClick={triggerScan} disabled={scanLoading}>
            {scanLoading ? <Loader2 className="spin" size={17} /> : <RefreshCw size={17} />}
            重新扫描
          </button>
          <label className="scan-retry-toggle" title="扫描时把处理失败或空结果的文件重新加入队列">
            <input
              type="checkbox"
              checked={retryFailedOnScan}
              onChange={(event) => setRetryFailedOnScan(event.target.checked)}
              disabled={scanLoading}
            />
            <span>重试失败文件</span>
          </label>
        </div>

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
          <b>{searchSummaryLabel(query, resultGroups.length, hasMoreResults)}</b>
          <span>{scope ? `范围：${scope}` : stats?.fts_tokenizer === 'trigram' ? '中文连续匹配' : '全文索引'}</span>
        </div>

        <div className="results-list">
          {resultGroups.map((group) => {
            const documentId = group.document_id;
            const expanded = Boolean(expandedDocuments[documentId]);
            const hitState = documentHits[documentId] || {
              results: [],
              hasMore: false,
              nextOffset: 0,
              loading: false,
              error: null,
            };
            return (
              <React.Fragment key={documentId}>
                <article
                  role="button"
                  tabIndex={0}
                  className={`result-row file-group-row ${expanded ? 'expanded' : ''}`}
                  onClick={() => toggleDocumentGroup(group)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      toggleDocumentGroup(group);
                    }
                  }}
                >
                  <div className="file-icon">{fileTypeLabel(group.ext)}</div>
                  <div className="result-body">
                    <div className="result-title">
                      <span>{group.title}</span>
                      <small>{formatNumber(group.match_count)} 处命中</small>
                    </div>
                    <p>{renderSnippet(group.snippet, query)}</p>
                    <div className="result-footer">
                      <span className="result-path">{group.rel_path}</span>
                      <span className="group-toggle-label">{expanded ? '收起' : '展开'}</span>
                    </div>
                  </div>
                </article>
                {expanded && (
                  <div className="group-hit-list">
                    {hitState.error && <div className="group-hit-state error">{hitState.error}</div>}
                    {hitState.loading && hitState.results.length === 0 && (
                      <div className="group-hit-state">
                        <Loader2 className="spin" size={15} />
                        正在加载文件内结果
                      </div>
                    )}
                    {hitState.results.map((result) => (
                      <article
                        role="listitem"
                        tabIndex={0}
                        className={`result-row hit-row ${selected?.match_id === result.match_id ? 'selected' : ''}`}
                        key={result.match_id}
                        onClick={() => setSelected(result)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            setSelected(result);
                          }
                        }}
                      >
                        <div className="hit-index">{result.page ? `P${result.page}` : result.line || result.ordinal + 1}</div>
                        <div className="result-body">
                          <div className="result-title">
                            <span>{result.page ? `PDF 第 ${result.page} 页` : result.line ? `第 ${result.line} 行` : '文本段落'}</span>
                            <small>{result.source}</small>
                          </div>
                          <p>{renderSnippet(result.snippet, query)}</p>
                          {result.citation && (
                            <div className="result-footer">
                              <span className="result-path">{result.rel_path}</span>
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
                            </div>
                          )}
                          {citationFor === result.match_id && result.citation && (
                            <div className="citation-box">{result.citation}</div>
                          )}
                        </div>
                      </article>
                    ))}
                    {hitState.hasMore && (
                      <button
                        type="button"
                        className="load-more-button document-load-more-button"
                        onClick={() => loadDocumentHits(group, hitState.nextOffset || hitState.results.length)}
                        disabled={hitState.loading}
                      >
                        {hitState.loading ? <Loader2 className="spin" size={16} /> : null}
                        加载此文件更多结果
                      </button>
                    )}
                  </div>
                )}
              </React.Fragment>
            );
          })}
          {query.trim() && !searching && resultGroups.length === 0 && (
            <div className="empty-state">
              <FileSearch size={24} />
              <span>没有命中结果</span>
            </div>
          )}
          {query.trim() && hasMoreResults && (
            <button
              type="button"
              className="load-more-button"
              onClick={loadMoreGroups}
              disabled={loadingMore || searching}
            >
              {loadingMore ? <Loader2 className="spin" size={16} /> : null}
              加载更多文件
            </button>
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
              </div>
            </div>

            {selectedPdfPageImageUrl ? (
              <PdfPagePreview src={selectedPdfPageImageUrl} page={selected.page} />
            ) : selectedPdfUrl ? (
              <iframe className="pdf-frame" title="PDF 预览" src={selectedPdfUrl} />
            ) : (
              <TextPreview context={textContext} query={query} />
            )}

            {(selectedPdfPageImageUrl || selectedPdfUrl) && <TextPreview context={textContext} query={query} compact />}
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

function PdfPagePreview({ src, page }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [src]);

  return (
    <div className="pdf-page-frame">
      {!loaded && !failed && <div className="pdf-page-state">正在加载第 {page} 页</div>}
      {failed && <div className="pdf-page-state error">无法加载 PDF 页面预览</div>}
      <img
        src={src}
        alt={`PDF 第 ${page} 页`}
        onLoad={() => setLoaded(true)}
        onError={() => setFailed(true)}
      />
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

function searchSummaryLabel(query, count, hasMore) {
  if (!query.trim()) return '输入关键词开始检索';
  return hasMore ? `已显示 ${count}+ 个文件` : `${count} 个文件`;
}

function fileTypeLabel(ext) {
  return ext === '.pdf' ? 'PDF' : String(ext || '').replace('.', '').toUpperCase();
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
    const quota = ocrQuotaLabel(stats);
    return quota ? `API ${quota}` : 'API';
  }
  const actual = stats?.ocr?.actual_device;
  if (actual?.startsWith('gpu')) return 'GPU';
  if (actual === 'cpu') return 'CPU';
  if (stats?.ocr?.cuda_available) return 'GPU 可用';
  return 'CPU';
}

function ocrQuotaLabel(stats) {
  const quota = stats?.ocr?.quota;
  const used = Number(quota?.used_pages);
  const limit = Number(quota?.daily_limit_pages);
  if (!Number.isFinite(used) || !Number.isFinite(limit) || limit <= 0) {
    return '';
  }
  return `${formatQuotaNumber(used)}/${formatQuotaNumber(limit)}`;
}

function formatQuotaNumber(value) {
  return String(Math.max(0, Math.trunc(Number(value) || 0)));
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

async function fetchSearchGroups(query, scope, offset) {
  const params = new URLSearchParams({
    q: query,
    scope,
    limit: String(SEARCH_GROUP_PAGE_SIZE),
    offset: String(offset),
  });
  return getJson(`${API}/search/groups?${params.toString()}`);
}

async function fetchDocumentSearchResults(query, documentId, offset) {
  const params = new URLSearchParams({
    q: query,
    limit: String(DOCUMENT_HIT_PAGE_SIZE),
    offset: String(offset),
  });
  return getJson(`${API}/search/document/${encodeURIComponent(documentId)}?${params.toString()}`);
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
  if (data?.method === 'windows-startfile' || data?.method === 'powershell-start-process') return '已请求系统打开';
  return '已请求打开本地文件';
}

async function responseErrorMessage(response, fallback) {
  const data = await response.json().catch(() => null);
  return data?.detail || data?.error || fallback;
}

createRoot(document.getElementById('root')).render(<App />);
