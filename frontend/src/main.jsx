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
  RotateCcw,
  Search,
  XCircle,
} from 'lucide-react';
import './styles.css';

const API = '/api';
const LOCAL_OPEN_HELPER = 'http://127.0.0.1:8765/open';
const SEARCH_GROUP_PAGE_SIZE = 200;
const DOCUMENT_HIT_PAGE_SIZE = 100;
const SEARCH_MODES = [
  { value: 'basic', label: '普通' },
  { value: 'line', label: '同一行' },
  { value: 'document', label: '同一文件' },
  { value: 'fuzzy', label: '模糊' },
];

function App() {
  const [query, setQuery] = useState('');
  const [searchMode, setSearchMode] = useState('basic');
  const [resultGroups, setResultGroups] = useState([]);
  const [expandedDocuments, setExpandedDocuments] = useState({});
  const [documentHits, setDocumentHits] = useState({});
  const [selected, setSelected] = useState(null);
  const [stats, setStats] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [failedDocuments, setFailedDocuments] = useState([]);
  const [failedTotal, setFailedTotal] = useState(0);
  const [events, setEvents] = useState([]);
  const [categories, setCategories] = useState([{ path: '', label: '全部资料' }]);
  const [scope, setScope] = useState('');
  const [citationFor, setCitationFor] = useState(null);
  const [textContext, setTextContext] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [hasMoreResults, setHasMoreResults] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [scanLoading, setScanLoading] = useState(false);
  const [vectorAction, setVectorAction] = useState('');
  const [vectorError, setVectorError] = useState('');
  const [retryFailedOnScan, setRetryFailedOnScan] = useState(false);
  const [jobAction, setJobAction] = useState({ id: null, type: '' });
  const [failedAction, setFailedAction] = useState({ id: null, type: '' });
  const [tokenForm, setTokenForm] = useState({
    paddleocr_api_token: '',
    deepseek_api_key: '',
    siliconflow_api_key: '',
  });
  const [tokenSaveState, setTokenSaveState] = useState({ saving: false, error: '', saved: false });
  const [openStatus, setOpenStatus] = useState(null);
  const searchRequestRef = useRef(0);
  const refreshStatusPromiseRef = useRef(null);

  useEffect(() => {
    refreshStatus();
    const timer = window.setInterval(refreshStatus, 10000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!query.trim()) {
      searchRequestRef.current += 1;
      setResultGroups([]);
      setExpandedDocuments({});
      setDocumentHits({});
      setSelected(null);
      setSearchError('');
      setHasMoreResults(false);
      setLoadingMore(false);
      return;
    }
    setSearching(true);
    const requestId = searchRequestRef.current + 1;
    searchRequestRef.current = requestId;
    const timer = window.setTimeout(async () => {
      try {
        const data = await fetchSearchGroups(query, scope, searchMode, 0);
        if (requestId !== searchRequestRef.current) return;
        const nextGroups = data.groups || [];
        setResultGroups(nextGroups);
        setExpandedDocuments({});
        setDocumentHits({});
        setSelected(null);
        setHasMoreResults(Boolean(data.has_more));
        setCitationFor(null);
        setSearchError('');
      } catch (error) {
        if (requestId !== searchRequestRef.current) return;
        setResultGroups([]);
        setExpandedDocuments({});
        setDocumentHits({});
        setSelected(null);
        setHasMoreResults(false);
        setSearchError(error.message || '搜索失败');
      } finally {
        if (requestId === searchRequestRef.current) setSearching(false);
      }
    }, 240);
    return () => window.clearTimeout(timer);
  }, [query, scope, searchMode]);

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
    if (refreshStatusPromiseRef.current) return refreshStatusPromiseRef.current;
    refreshStatusPromiseRef.current = (async () => {
    const [statsData, jobsData, failedData, eventsData, categoriesData] = await Promise.all([
      getJson(`${API}/stats`).catch(() => null),
      getJson(`${API}/jobs`).catch(() => ({ jobs: [] })),
      getJson(`${API}/failed-documents?limit=12`).catch(() => ({ documents: [], total: 0 })),
      getJson(`${API}/events`).catch(() => ({ events: [] })),
      getJson(`${API}/categories`).catch(() => ({ categories: [{ path: '', label: '全部资料' }] })),
    ]);
    if (statsData) setStats(statsData);
    setJobs(jobsData.jobs || []);
    setFailedDocuments(failedData.documents || []);
    setFailedTotal(Number(failedData.total || 0));
    setEvents(eventsData.events || []);
    setCategories(categoriesData.categories || [{ path: '', label: '全部资料' }]);
  }

    )();
    try {
      await refreshStatusPromiseRef.current;
    } finally {
      refreshStatusPromiseRef.current = null;
    }
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

  async function triggerVectorRebuild(force = false) {
    if (vectorAction) return;
    setVectorAction('start');
    setVectorError('');
    try {
      await postJson(`${API}/vector/rebuild?force=${force ? 'true' : 'false'}`);
      await refreshStatus();
    } catch (error) {
      setVectorError(error.message || '无法开始构建模糊索引');
    } finally {
      setVectorAction('');
    }
  }

  async function cancelVectorIndex() {
    if (vectorAction) return;
    setVectorAction('stop');
    setVectorError('');
    try {
      await postJson(`${API}/vector/cancel`);
      await refreshStatus();
    } catch (error) {
      setVectorError(error.message || '无法中止模糊索引构建');
    } finally {
      setVectorAction('');
    }
  }

  async function controlJob(job, action) {
    if (!job?.id || jobAction.id) return;
    setJobAction({ id: job.id, type: action });
    try {
      await postJson(`${API}/jobs/${job.id}/${action}`);
      await refreshStatus();
    } finally {
      setJobAction({ id: null, type: '' });
    }
  }

  async function retryFailedDocument(document) {
    if (!document?.id || failedAction.id) return;
    setFailedAction({ id: document.id, type: 'single' });
    try {
      await postJson(`${API}/documents/${encodeURIComponent(document.id)}/retry`);
      await refreshStatus();
    } finally {
      setFailedAction({ id: null, type: '' });
    }
  }

  async function retryAllFailedDocuments() {
    if (failedAction.id || failedTotal <= 0) return;
    setFailedAction({ id: 'all', type: 'all' });
    try {
      await postJson(`${API}/failed-documents/retry`);
      await refreshStatus();
    } finally {
      setFailedAction({ id: null, type: '' });
    }
  }

  async function saveApiTokens(event) {
    event.preventDefault();
    setTokenSaveState({ saving: true, error: '', saved: false });
    try {
      await postJson(`${API}/config/tokens`, {
        paddleocr_api_token: tokenForm.paddleocr_api_token,
        deepseek_api_key: tokenForm.deepseek_api_key,
        siliconflow_api_key: tokenForm.siliconflow_api_key,
      });
      setTokenForm({ paddleocr_api_token: '', deepseek_api_key: '', siliconflow_api_key: '' });
      await refreshStatus();
      setTokenSaveState({ saving: false, error: '', saved: true });
      window.setTimeout(() => setTokenSaveState((current) => ({ ...current, saved: false })), 2400);
    } catch (error) {
      setTokenSaveState({ saving: false, error: error.message || '保存失败', saved: false });
    }
  }

  async function loadMoreGroups() {
    if (loadingMore || searching || !hasMoreResults || !query.trim()) return;
    setLoadingMore(true);
    const requestId = searchRequestRef.current;
    const offset = resultGroups.length;
    try {
      const data = await fetchSearchGroups(query, scope, searchMode, offset);
      if (requestId !== searchRequestRef.current) return;
      const nextGroups = data.groups || [];
      setResultGroups((current) => {
        const seen = new Set(current.map((item) => item.document_id));
        return [...current, ...nextGroups.filter((item) => !seen.has(item.document_id))];
      });
      setHasMoreResults(Boolean(data.has_more));
      setSearchError('');
    } catch (error) {
      if (requestId === searchRequestRef.current) setSearchError(error.message || '加载更多结果失败');
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
      const data = await fetchDocumentSearchResults(query, documentId, searchMode, offset);
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
      const data = await openLocalDocument(selected, selectedPdfUrl);
      setOpenStatus({ tone: 'ok', message: openSuccessMessage(data) });
      window.setTimeout(() => setOpenStatus(null), 2200);
    } catch (error) {
      setOpenStatus({ tone: 'error', message: error.message || '无法打开本地文件' });
    }
  }

  const activeJob = stats?.latest_job;
  const selectedPdfUrl = useMemo(() => {
    if (!selected) return '';
    if (!hasPdfPreview(selected)) return '';
    const page = selected.page ? `#page=${selected.page}&search=${encodeURIComponent(query)}` : '';
    return `${API}/files/${selected.document_id}/pdf${page}`;
  }, [selected, query]);
  const selectedPdfPageImageUrl = useMemo(() => {
    if (!selected || !hasPdfPreview(selected) || !selected.page) return '';
    const params = new URLSearchParams({ page: String(selected.page) });
    if (selected.match_id) params.set('match_id', String(selected.match_id));
    if (query.trim()) params.set('q', query.trim());
    return `${API}/files/${selected.document_id}/page-image?${params.toString()}`;
  }, [selected, query]);
  const activeSearchMode = SEARCH_MODES.find((mode) => mode.value === searchMode) || SEARCH_MODES[0];
  const showSearchProgress = Boolean(query.trim() && searching);
  const vectorActive = vectorIndexActive(stats);
  const missingSiliconFlowKey = needsSiliconFlowKey(stats);

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <FileSearch size={20} />
          </div>
          <div>
            <h1>本地资料检索</h1>
            <p>PP-OCRv6 API · SiliconFlow 向量</p>
          </div>
        </div>

        <section className="status-stack">
          <Metric icon={<Database size={18} />} label="已索引" value={stats?.documents?.ready ?? 0} />
          <Metric icon={<FolderSync size={18} />} label="待处理" value={stats?.jobs?.queued ?? 0} tone="amber" />
          <OcrDeviceMetric
            stats={stats}
            tokenForm={tokenForm}
            tokenSaveState={tokenSaveState}
            onTokenChange={setTokenForm}
            onSave={saveApiTokens}
          />
          <Metric icon={<Gauge size={18} />} label="资源策略" value={resourceLimitLabel(stats)} tone="blue" />
          <Metric
            icon={<Search size={18} />}
            label="模糊索引"
            value={vectorIndexLabelDetailed(stats)}
            tone={vectorIndexReady(stats) ? 'green' : 'blue'}
          />
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
          <div className="vector-settings" aria-label="模糊索引设置">
            <div className="vector-setting-head">
              <span>
                <Search size={15} />
                模糊索引
              </span>
              <small>{vectorControlStatusDetailed(stats)}</small>
            </div>
            {missingSiliconFlowKey && (
              <SiliconFlowKeyForm
                tokenForm={tokenForm}
                tokenSaveState={tokenSaveState}
                onTokenChange={setTokenForm}
                onSave={saveApiTokens}
              />
            )}
            <div className="vector-action-row">
              <button
                className="vector-action-button start"
                type="button"
                onClick={() => triggerVectorRebuild(false)}
                disabled={Boolean(vectorAction) || vectorActive || !stats?.vector?.enabled || missingSiliconFlowKey}
              >
                {vectorAction === 'start' ? <Loader2 className="spin" size={14} /> : <Search size={14} />}
                {vectorStartLabel(stats, vectorActive)}
              </button>
              <button
                className="vector-action-button stop"
                type="button"
                onClick={cancelVectorIndex}
                disabled={Boolean(vectorAction) || !vectorActive}
              >
                {vectorAction === 'stop' ? <Loader2 className="spin" size={14} /> : <XCircle size={14} />}
                中止构建
              </button>
            </div>
            {vectorError && <div className="vector-error">{vectorError}</div>}
          </div>
        </div>

        <section className="failed-documents">
          <div className="section-title failed-title">
            <span>
              <AlertTriangle size={16} />
              失败文件
            </span>
            {failedTotal > 0 && (
              <button
                type="button"
                onClick={retryAllFailedDocuments}
                disabled={Boolean(failedAction.id)}
                title="将所有失败或空结果文件重新加入处理队列"
              >
                {failedAction.id === 'all' ? <Loader2 className="spin" size={13} /> : <RotateCcw size={13} />}
                全部重试
              </button>
            )}
          </div>
          {failedDocuments.length ? (
            <div className="failed-list">
              {failedDocuments.map((document) => (
                <div className="failed-row" key={document.id}>
                  <div className="failed-row-main">
                    <b title={document.rel_path}>{failedDocumentLabel(document)}</b>
                    <span title={failedDocumentReason(document)}>{failedDocumentReason(document)}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => retryFailedDocument(document)}
                    disabled={Boolean(failedAction.id)}
                    title="重新处理此文件"
                  >
                    {failedAction.id === document.id ? <Loader2 className="spin" size={13} /> : <RotateCcw size={13} />}
                    重试
                  </button>
                </div>
              ))}
              {failedTotal > failedDocuments.length && (
                <div className="failed-more">还有 {failedTotal - failedDocuments.length} 个失败文件未显示</div>
              )}
            </div>
          ) : (
            <p className="muted">暂无失败文件</p>
          )}
        </section>

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
              <div className="job-actions">
                <button
                  type="button"
                  onClick={() => controlJob(activeJob, 'cancel')}
                  disabled={Boolean(jobAction.id)}
                  title="中止当前处理任务"
                >
                  {jobAction.id === activeJob.id && jobAction.type === 'cancel' ? (
                    <Loader2 className="spin" size={14} />
                  ) : (
                    <XCircle size={14} />
                  )}
                  中止
                </button>
                <button
                  type="button"
                  onClick={() => controlJob(activeJob, 'restart')}
                  disabled={Boolean(jobAction.id)}
                  title="中止当前任务并重新加入处理队列"
                >
                  {jobAction.id === activeJob.id && jobAction.type === 'restart' ? (
                    <Loader2 className="spin" size={14} />
                  ) : (
                    <RotateCcw size={14} />
                  )}
                  重新开始
                </button>
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

        <div className="search-mode-bar" role="group" aria-label="匹配方式">
          <span>匹配</span>
          {SEARCH_MODES.map((mode) => (
            <button
              type="button"
              key={mode.value}
              className={searchMode === mode.value ? 'active' : ''}
              onClick={() => setSearchMode(mode.value)}
            >
              {mode.label}
            </button>
          ))}
        </div>

        <div className="result-summary">
          <b>{searchSummaryLabel(query, resultGroups.length, hasMoreResults)}</b>
          <span>{searchModeDetail(activeSearchMode, scope, stats)}</span>
        </div>

        <div className="results-list">
          {showSearchProgress && <SearchProgress query={query} />}
          {!showSearchProgress && searchError && (
            <div className="search-error">
              <AlertTriangle size={18} />
              <span>{searchError}</span>
            </div>
          )}
          {!showSearchProgress && resultGroups.map((group) => {
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
          {!showSearchProgress && query.trim() && !searching && !searchError && resultGroups.length === 0 && (
            <div className="empty-state">
              <FileSearch size={24} />
              <span>没有命中结果</span>
            </div>
          )}
          {!showSearchProgress && query.trim() && hasMoreResults && (
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

function OcrDeviceMetric({ stats, tokenForm, tokenSaveState, onTokenChange, onSave }) {
  const tokens = stats?.ocr?.tokens || {};
  const needsPaddleToken = stats?.ocr?.engine === 'api' && !tokens.paddleocr_api_token_configured;
  const needsDeepSeekKey = stats?.ocr?.engine === 'api' && !tokens.deepseek_api_key_configured;
  const needsSetup = needsPaddleToken || needsDeepSeekKey;
  if (!needsSetup) {
    return (
      <Metric
        icon={<Cpu size={18} />}
        label="OCR 设备"
        value={ocrDeviceLabel(stats)}
        tone={isGpuReady(stats) ? 'blue' : 'green'}
      />
    );
  }

  const canSave =
    (!needsPaddleToken || tokenForm.paddleocr_api_token.trim()) &&
    (!needsDeepSeekKey || tokenForm.deepseek_api_key.trim()) &&
    !tokenSaveState.saving;

  return (
    <form className="metric token-metric" onSubmit={onSave}>
      <Cpu size={18} />
      <div>
        <span>OCR 设备</span>
        <b>配置 API Token</b>
        <div className="token-input-stack">
          {needsPaddleToken ? (
            <input
              type="password"
              value={tokenForm.paddleocr_api_token}
              onChange={(event) =>
                onTokenChange((current) => ({ ...current, paddleocr_api_token: event.target.value }))
              }
              placeholder="PADDLEOCR_API_TOKEN"
              autoComplete="off"
            />
          ) : (
            <small>PaddleOCR 已配置</small>
          )}
          {needsDeepSeekKey ? (
            <input
              type="password"
              value={tokenForm.deepseek_api_key}
              onChange={(event) =>
                onTokenChange((current) => ({ ...current, deepseek_api_key: event.target.value }))
              }
              placeholder="DEEPSEEK_API_KEY"
              autoComplete="off"
            />
          ) : (
            <small>DeepSeek 已配置</small>
          )}
          <button type="submit" disabled={!canSave}>
            {tokenSaveState.saving ? <Loader2 className="spin" size={13} /> : null}
            保存
          </button>
          {tokenSaveState.error && <small className="token-error">{tokenSaveState.error}</small>}
          {tokenSaveState.saved && <small className="token-ok">已保存</small>}
        </div>
      </div>
    </form>
  );
}

function SiliconFlowKeyForm({ tokenForm, tokenSaveState, onTokenChange, onSave }) {
  const canSave = Boolean(tokenForm.siliconflow_api_key.trim()) && !tokenSaveState.saving;
  return (
    <form className="vector-token-form" onSubmit={onSave}>
      <label>
        <span>请填写 SiliconFlow API Key</span>
        <input
          type="password"
          value={tokenForm.siliconflow_api_key}
          onChange={(event) =>
            onTokenChange((current) => ({ ...current, siliconflow_api_key: event.target.value }))
          }
          placeholder="SILICONFLOW_API_KEY"
          autoComplete="off"
        />
      </label>
      <button type="submit" disabled={!canSave}>
        {tokenSaveState.saving ? <Loader2 className="spin" size={13} /> : null}
        保存
      </button>
      {tokenSaveState.error && <small className="token-error">{tokenSaveState.error}</small>}
      {tokenSaveState.saved && <small className="token-ok">已保存</small>}
    </form>
  );
}

function SearchProgress({ query }) {
  const text = String(query || '').trim();
  return (
    <div className="search-progress" role="status" aria-live="polite">
      <Loader2 className="spin" size={22} />
      <strong>正在搜索</strong>
      <span>{text ? `正在匹配“${text}”` : '正在准备搜索'}</span>
      <div className="search-progress-track" aria-hidden="true">
        <span />
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
  const terms = parseSearchTerms(query);
  if (!terms.length) return value;
  const lower = value.toLocaleLowerCase();
  const ranges = [];
  for (const term of terms) {
    const needle = term.toLocaleLowerCase();
    let start = 0;
    while (needle && start < lower.length) {
      const index = lower.indexOf(needle, start);
      if (index < 0) break;
      ranges.push([index, index + term.length]);
      start = index + Math.max(1, term.length);
    }
  }
  const selectedRanges = [];
  ranges
    .sort((a, b) => a[0] - b[0] || b[1] - b[0] - (a[1] - a[0]))
    .forEach((range) => {
      if (selectedRanges.length && range[0] < selectedRanges[selectedRanges.length - 1][1]) return;
      selectedRanges.push(range);
    });
  if (!selectedRanges.length) return value;
  const parts = [];
  let cursor = 0;
  selectedRanges.forEach(([start, end], index) => {
    if (start > cursor) parts.push(<React.Fragment key={`text-${index}`}>{value.slice(cursor, start)}</React.Fragment>);
    parts.push(<mark key={`mark-${index}`}>{value.slice(start, end)}</mark>);
    cursor = end;
  });
  if (cursor < value.length) parts.push(<React.Fragment key="tail">{value.slice(cursor)}</React.Fragment>);
  return (
    <>
      {parts}
    </>
  );
}

function parseSearchTerms(value) {
  const text = String(value || '').trim();
  if (!text) return [];
  const terms = [];
  let current = '';
  let quoteEnd = '';
  const quotePairs = { '"': '"', "'": "'", '“': '”', '‘': '’' };
  const separators = new Set([',', '，', '、', ';', '；']);
  const flush = () => {
    const term = current.trim().replace(/\s+/g, ' ');
    current = '';
    if (term) terms.push(term);
  };
  for (const char of text) {
    if (quoteEnd) {
      if (char === quoteEnd) quoteEnd = '';
      else current += char;
      continue;
    }
    if (quotePairs[char]) {
      quoteEnd = quotePairs[char];
      continue;
    }
    if (/\s/.test(char) || separators.has(char)) {
      flush();
      continue;
    }
    current += char;
  }
  flush();
  return terms;
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

function searchModeDetail(activeSearchMode, scope, stats) {
  if (activeSearchMode.value === 'fuzzy') {
    const vector = stats?.vector;
    const source = vectorProviderLabel(vector);
    const base = vector?.available ? `FAISS 4bit 向量索引 · ${source}` : `模糊索引未就绪 · ${source}`;
    return scope ? `${base} · 范围：${scope}` : base;
  }
  const indexLabel = scope ? `范围：${scope}` : stats?.fts_tokenizer === 'trigram' ? '中文连续匹配' : '全文索引';
  if (activeSearchMode.value === 'basic') return indexLabel;
  return `${activeSearchMode.label} · ${indexLabel}`;
}

function fileTypeLabel(ext) {
  return ext === '.pdf' ? 'PDF' : String(ext || '').replace('.', '').toUpperCase();
}

function hasPdfPreview(result) {
  if (!result) return false;
  if (result.ext === '.pdf') return true;
  return result.ext === '.caj' && Boolean(result.searchable_pdf);
}

function resourceLimitLabel(stats) {
  const limits = stats?.resources?.limits;
  if (!limits) return '自动';
  return `${limits.max_file_mb}MB`;
}

function vectorIndexLabel(stats) {
  const vector = stats?.vector;
  if (!vector?.enabled) return '已关闭';
  const embedded = Number(vector.embedded_documents || 0);
  const total = Number(vector.documents || 0);
  if (vector.processing || vector.queued) {
    return `${embedded}/${total} 处理中`;
  }
  return `${embedded}/${total}`;
}

function vectorIndexReady(stats) {
  const vector = stats?.vector;
  return Boolean(vector?.available && !vector?.index_dirty);
}

function vectorIndexActive(stats) {
  const vector = stats?.vector;
  return Boolean(Number(vector?.queued || 0) || Number(vector?.processing || 0));
}

function vectorControlStatus(stats) {
  const vector = stats?.vector;
  if (!vector?.enabled) return '已关闭';
  const source = vectorProviderLabel(vector);
  const embedded = Number(vector.embedded_documents || 0);
  const total = Number(vector.documents || 0);
  const queued = Number(vector.queued || 0);
  const processing = Number(vector.processing || 0);
  if (queued || processing) return `${source} · ${embedded}/${total} · 处理中 ${processing}/${queued}`;
  if (vectorIndexReady(stats)) return `${source} · ${embedded}/${total} · 可搜索`;
  return `${source} · ${embedded}/${total} · 未就绪`;
}

function vectorStartLabel(stats, active) {
  if (active) return '构建中';
  const vector = stats?.vector;
  if (!vector?.enabled) return '已关闭';
  if (needsSiliconFlowKey(stats)) return '先填写 Key';
  if (vectorIndexReady(stats)) return '更新索引';
  return Number(vector.embeddings || 0) > 0 ? '继续构建' : '开始构建';
}

function vectorProviderLabel(vector) {
  if (vector?.provider === 'siliconflow') return 'SiliconFlow';
  return vector?.provider === 'api' ? 'API' : '本机';
}

function needsSiliconFlowKey(stats) {
  const vector = stats?.vector;
  if (!vector?.enabled || vector?.provider !== 'siliconflow') return false;
  const tokens = stats?.ocr?.tokens || {};
  return !Boolean(vector.api_key_configured || tokens.siliconflow_api_key_configured);
}

function vectorIndexLabelDetailed(stats) {
  const vector = stats?.vector;
  if (!vector?.enabled) return '已关闭';
  const embedded = Number(vector.embedded_documents || 0);
  const total = Number(vector.documents || 0);
  const failed = Number(vector.failed || 0);
  const job = activeVectorJob(stats);
  const failedText = failed > 0 ? ` · 失败${failed}` : '';
  if (vector.processing || vector.queued) {
    return job
      ? `${embedded}/${total}${failedText} · 当前${progressPercent(job.progress)}`
      : `${embedded}/${total}${failedText} · 处理中`;
  }
  return `${embedded}/${total}${failedText}`;
}

function vectorControlStatusDetailed(stats) {
  const vector = stats?.vector;
  if (!vector?.enabled) return '已关闭';
  const source = vectorProviderLabel(vector);
  const embedded = Number(vector.embedded_documents || 0);
  const total = Number(vector.documents || 0);
  const queued = Number(vector.queued || 0);
  const processing = Number(vector.processing || 0);
  const failed = Number(vector.failed || 0);
  const chunks = Number(vector.chunks || 0);
  const embeddings = Number(vector.embeddings || 0);
  const requestRate = Number(vector.request_rate_limit_per_second || 0);
  const batchSize = Number(vector.embedding_batch_size || 0);
  const concurrency = Number(vector.embedding_concurrency || 0);
  const tokenLimit = Number(vector.tokens_per_minute_limit || 0);
  const job = activeVectorJob(stats);
  const apiIssue = vectorApiIssueLabel(vector);
  const parts = [`${source}`, `文件 ${embedded}/${total}`];
  if (requestRate > 0) parts.push(`限速 ${formatRate(requestRate)} 次/秒`);
  if (concurrency > 0) {
    parts.push(batchSize > 0 ? `批量 ${batchSize} · 并发 ${concurrency}` : `按 token 打包 · 并发 ${concurrency}`);
  }
  if (tokenLimit > 0) parts.push(`TPM ${formatNumber(tokenLimit)}`);
  if (needsSiliconFlowKey(stats)) parts.push('缺少 API Key');
  if (apiIssue) parts.push(apiIssue);
  if (job) {
    const current = vectorJobProgressText(job);
    parts.push(current ? `当前 ${progressPercent(job.progress)} (${current})` : `当前 ${progressPercent(job.progress)}`);
  }
  if (chunks > 0) parts.push(`行 ${formatNumber(embeddings)}/${formatNumber(chunks)}`);
  if (failed > 0) parts.push(`失败 ${failed}`);
  if (queued || processing) parts.push(`队列 ${processing}/${queued}`);
  if (!queued && !processing && vectorIndexReady(stats)) parts.push('可搜索');
  if (!queued && !processing && !vectorIndexReady(stats)) parts.push('未完成');
  return parts.join(' · ');
}

function activeVectorJob(stats) {
  const job = stats?.latest_job;
  if (!job || job.type !== 'vector' || job.status !== 'processing') return null;
  return job;
}

function progressPercent(value) {
  const number = Number(value || 0);
  const clamped = Math.max(0, Math.min(1, Number.isFinite(number) ? number : 0));
  return `${Math.round(clamped * 100)}%`;
}

function formatRate(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return '0';
  if (Math.abs(number - Math.round(number)) < 0.01) return String(Math.round(number));
  return number.toFixed(1);
}

function vectorJobProgressText(job) {
  const message = String(job?.message || '');
  const match = message.match(/(\d+)\s*\/\s*(\d+)/);
  return match ? `${match[1]}/${match[2]}` : '';
}

function vectorApiIssueLabel(vector) {
  if (!['api', 'siliconflow'].includes(vector?.provider)) return '';
  const error = String(vector?.index_error || '').toLowerCase();
  if (!error) return '';
  if (error.includes('siliconflow api key')) {
    return '请填写 SiliconFlow API Key';
  }
  if (error.includes('siliconflow')) {
    return 'SiliconFlow API 不可用';
  }
  if (error.includes('health check') || error.includes('http 530') || error.includes('cloudflare tunnel')) {
    return 'API 不可用，等待恢复';
  }
  return '';
}

function jobLabel(job) {
  const title = job.title || job.rel_path;
  if (job.status === 'failed' && job.error) return `${title}: ${job.error}`;
  return title;
}

function jobTooltip(job) {
  return job.error || job.message || job.rel_path;
}

function failedDocumentLabel(document) {
  return document.title || document.rel_path || document.id;
}

function failedDocumentReason(document) {
  if (document.status === 'empty') return '未抽取到文字，可重试处理';
  return document.error || '处理失败，可重试处理';
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
  if (!response.ok) throw new Error(await responseErrorMessage(response, `GET ${url} failed`));
  return response.json();
}

async function fetchSearchGroups(query, scope, mode, offset) {
  const params = new URLSearchParams({
    q: query,
    scope,
    mode,
    limit: String(SEARCH_GROUP_PAGE_SIZE),
    offset: String(offset),
  });
  return getJson(`${API}/search/groups?${params.toString()}`);
}

async function fetchDocumentSearchResults(query, documentId, mode, offset) {
  const params = new URLSearchParams({
    q: query,
    mode,
    limit: String(DOCUMENT_HIT_PAGE_SIZE),
    offset: String(offset),
  });
  return getJson(`${API}/search/document/${encodeURIComponent(documentId)}?${params.toString()}`);
}

async function postJson(url, body) {
  const options = body
    ? {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }
    : { method: 'POST' };
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(await responseErrorMessage(response, `POST ${url} failed`));
  return response.json();
}

async function openLocalDocument(result, pdfUrl = '') {
  const errors = [];
  try {
    const response = await fetch(LOCAL_OPEN_HELPER, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rel_path: result.rel_path }),
    });
    if (!response.ok) {
      throw new Error(await responseErrorMessage(response, '本地打开助手返回错误'));
    }
    return response.json().catch(() => ({ ok: true }));
  } catch (error) {
    errors.push(error);
  }

  try {
    const response = await fetch(`${API}/files/${result.document_id}/open`, { method: 'POST' });
    if (response.ok) return response.json().catch(() => ({ ok: true }));
    throw new Error(await responseErrorMessage(response, '后端无法打开本地文件'));
  } catch (error) {
    errors.push(error);
  }

  if (pdfUrl) {
    const opened = window.open(pdfUrl, '_blank');
    if (opened) {
      opened.opener = null;
      return { ok: true, method: 'browser-pdf' };
    }
    window.location.assign(pdfUrl);
    return { ok: true, method: 'browser-pdf-current-tab' };
  }

  const detail = errors.map((error) => error?.message).filter(Boolean).join('；');
  throw new Error(detail || '无法打开本地文件，请运行 scripts\\start.ps1 启动本地打开助手');
}

function openSuccessMessage(data) {
  if (data?.method === 'notepad') return '已调用 Notepad 打开';
  if (data?.method === 'windows-startfile' || data?.method === 'powershell-start-process') return '已请求系统打开';
  if (data?.method === 'browser-pdf' || data?.method === 'browser-pdf-current-tab') return '已在浏览器中打开 PDF';
  return '已请求打开本地文件';
}

async function responseErrorMessage(response, fallback) {
  const data = await response.json().catch(() => null);
  return data?.detail || data?.error || fallback;
}

createRoot(document.getElementById('root')).render(<App />);
