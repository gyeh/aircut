'use strict';

const $ = selector => document.querySelector(selector);
const main = $('#main');
const dialog = $('#dialog');
const dialogBody = $('#dialog-body');
const audio = $('#audio');
const icons = {
  phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18.5h4"/>',
  rss: '<circle cx="5.5" cy="18.5" r="1.5" fill="currentColor"/><path d="M4 10a10 10 0 0 1 10 10M4 4a16 16 0 0 1 16 16"/>',
  bookmark: '<path d="M6 4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v17l-6-4-6 4z"/>',
  trash: '<path d="M3 6h18M9 6V4h6v2M6 6l1 14h10l1-14M10 10v6m4-6v6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  back: '<path d="M20 12H4m6 6-6-6 6-6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  refresh: '<path d="M20 5v5h-5M4 19v-5h5M5.5 9a7 7 0 0 1 12.2-3L20 9M4 15l2.3 3A7 7 0 0 0 18.5 15"/>',
  sound: '<path d="M3 10v4m4-8v12m5-16v20m5-15v10m4-7v4"/>',
  pause: '<path d="M5 4h5v16H5zm9 0h5v16h-5z" fill="currentColor" stroke-width="0"/>',
  play: '<path d="m7 4 13 8-13 8z" fill="currentColor" stroke-width="1"/>',
  settings: '<path d="M3 6h8m4 0h6M3 12h3m4 0h11M3 18h11m4 0h3"/><circle cx="13" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
  check: '<path d="m4 13 5 5L20 6"/>',
  library: '<path d="M4 4h3v16H4zM10 4h3v16h-3zm6 1 3-1 4 15-3 1z"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 5V4a1 1 0 0 0-1-1H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
};
function icon(name) {
  return `<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${icons[name] || ''}</svg>`;
}
document.querySelectorAll('[data-icon]').forEach(node => { node.innerHTML = icon(node.dataset.icon); });
function escapeHtml(value) {
  const replacements = {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'};
  return String(value ?? '').replace(/[&<>"']/g, c => replacements[c]);
}
function httpUrl(value, sameOrigin = false) {
  if (!value) return '';
  try {
    const url = new URL(value, location.origin);
    return /^https?:$/.test(url.protocol) && (!sameOrigin || url.origin === location.origin) ? url.href : '';
  } catch { return ''; }
}
function route() {
  const parts = location.hash.slice(1).split('/');
  try { return {page: parts[0] || 'library', id: decodeURIComponent(parts[1] || ''), fromSaved: parts[2] === 'saved', fromFeed: parts[2] === 'feed' ? decodeURIComponent(parts[3] || '') : ''}; }
  catch { return {page: 'library', id: ''}; }
}
const idPart = value => encodeURIComponent(String(value));
const episodeLink = id => `#episode/${idPart(id)}`;
const feedLink = id => `#feed/${idPart(id)}`;
const sameId = (a, b) => String(a) === String(b);
const findFeed = id => state?.feeds.find(f => sameId(f.id, id));
function episodeListLink(view) {
  if (view.fromSaved && supportsSavedEpisodes()) return '#saved';
  return view.fromFeed && findFeed(view.fromFeed) ? feedLink(view.fromFeed) : '#library';
}
function button(action, label, glyph = '', style = 'button') {
  return `<button type="button" class="${style}" data-action="${action}">${glyph ? icon(glyph) : ''}${escapeHtml(label)}</button>`;
}
function countLabel(n, single, plural = single + 's') { return `${n} ${n === 1 ? single : plural}`; }
function dateLabel(value) {
  if (!value) return '';
  const date = new Date(typeof value === 'number' && value < 1e11 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
}
function timeLabel(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return (hours ? `${hours}:${String(minutes % 60).padStart(2, '0')}` : String(minutes)) + ':' + String(seconds % 60).padStart(2, '0');
}
function cover(title, imageUrl, id) {
  const tint = [...String(id)].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 3;
  const source = httpUrl(imageUrl);
  return `<span class="art t${tint}" aria-hidden="true">${escapeHtml([...String(title || 'A')][0])}${source ? `<img alt="" loading="lazy" referrerpolicy="no-referrer" src="${escapeHtml(source)}">` : ''}</span>`;
}
document.addEventListener('error', event => {
  if (event.target.matches?.('.art img')) event.target.hidden = true;
}, true);
let state, detail, detailRequest = 0, stateRequest = 0, lastState = '', currentDialog = '', filter = 'all';
let toastTimer, refreshing = false, waitingTunnel = false, deviceSignature = '', dialogFeed;

async function api(path, method = 'GET', body) {
  const options = {method, cache:'no-store', headers:{Accept:'application/json'}};
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    if (body !== undefined) options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error(response.ok ? 'The server returned an unreadable response.' : `The server returned HTTP ${response.status}.`); }
  if (!response.ok) throw Object.assign(new Error(data.error || `Request failed (HTTP ${response.status}).`), {status:response.status});
  return data;
}
function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 4500);
}
function setConnection(online) {
  $('#server-label').textContent = online ? 'Server online' : 'Server unavailable';
  $('#server-light').classList.toggle('online', online);
}
function showError(target, message) {
  target.innerHTML = `<p class="connection-error" role="alert">${escapeHtml(message)}</p>${button('reload', 'Try again', 'refresh', 'button outline')}`;
}
async function loadState(force = false) {
  const request = ++stateRequest;
  try {
    const fresh = await api('/api/state');
    if (request !== stateRequest) return;
    const fingerprint = JSON.stringify(fresh);
    const changed = lastState !== fingerprint;
    const previous = state;
    state = fresh;
    lastState = fingerprint;
    if (changed && previous) {
      const current = new Set(fresh.episodes.map(e => e.id));
      for (const episode of previous.episodes) {
        if (!current.has(episode.id)) episodeRemoved(episode.id);
      }
    }
    updatePlayerControls();
    if (playerSettingsSupported()) playheads.forEach(flushProgress);
    setConnection(true);
    if (changed || force) {
      const focusedPlay = document.activeElement?.closest('.row-play');
      renderRoute();
      if (focusedPlay && !focusedPlay.isConnected) {
        playControl(focusedPlay.dataset.id)?.focus({preventScroll:true});
      }
    }
    if (currentDialog === 'device') renderDevice();
  } catch (err) {
    if (request !== stateRequest) return;
    setConnection(false);
    if (!state) showError(main, err.message);
    else if (force) toast(err.message);
  }
}
function statusPill(episode) {
  const labels = {ready:'Ready', queued:'Queued', processing:'Processing', failed:'Failed'};
  const status = Object.hasOwn(labels, episode.status) ? episode.status : 'queued';
  return `<span class="state-pill ${status}">${labels[status]}</span>`;
}
function supportsSavedEpisodes() {
  return Object.hasOwn(state.settings, 'skip_back_seconds');
}
function saveButton(episode, withText = false) {
  if (!supportsSavedEpisodes()) return '';
  const saved = Boolean(episode.is_saved);
  const label = saved ? 'Remove from Saved' : 'Save for later';
  const style = withText ? 'button outline' : 'icon-button';
  return `<button type="button" class="save-episode ${style}" data-action="save-episode" data-id="${escapeHtml(episode.id)}" aria-label="Save ${escapeHtml(episode.title)} for later" aria-pressed="${saved}" title="${label}">${icon('bookmark')}${withText ? `<span>${saved ? 'Saved' : 'Save'}</span>` : ''}</button>`;
}
function deleteButton(episode, withText = false) {
  if (!state.settings.episode_deletion) return '';
  const label = `Delete ${escapeHtml(episode.title)}`;
  return `<button type="button" class="delete-episode ${withText ? 'button outline' : 'icon-button'}" data-action="delete-episode" data-id="${escapeHtml(episode.id)}" aria-label="${label}" title="${label}">${icon('trash')}${withText ? 'Delete' : ''}</button>`;
}
function openEpisodeDelete(id) {
  const episode = episodeForPlayback(id);
  if (!episode) return;
  openDialog('delete-episode', 'trash', `<h2 id="dialog-title">Delete episode?</h2>
    <p id="delete-description">This permanently removes <strong>${escapeHtml(episode.title)}</strong> from Aircut and deletes its local audio, transcript, and notes.</p>
    <p class="form-error" role="alert"></p><div class="action-list">${button('close-dialog', 'Cancel', '', 'button outline')}
    <button class="button danger" type="button" data-action="confirm-delete-episode" data-id="${escapeHtml(id)}">Delete episode</button></div>`);
  dialog.setAttribute('aria-describedby', 'delete-description');
  dialogBody.querySelector('[data-action="close-dialog"]').focus();
}
function episodeRemoved(id) {
  discardEpisodePlayback(id);
  const confirmation = dialogBody.querySelector('[data-action="confirm-delete-episode"]');
  if (dialog.open && confirmation && sameId(confirmation.dataset.id, id)) dialog.close();
  const view = route();
  if (view.page === 'episode' && sameId(view.id, id)) {
    detailRequest++;
    detail = undefined;
    history.replaceState(null, '', episodeListLink(view));
  }
}
function removeFromLibrary(id) {
  stateRequest++;
  state.episodes = state.episodes.filter(episode => !sameId(episode.id, id));
  lastState = JSON.stringify(state);
  episodeRemoved(id);
  renderRoute();
  if (document.activeElement === document.body) main.focus({preventScroll:true});
}
async function confirmEpisodeDelete(control) {
  const id = control.dataset.id;
  control.disabled = true;
  let result;
  try {
    result = await api(`/api/episodes/${idPart(id)}`, 'DELETE');
  } catch (err) {
    if (err.status !== 404) {
      control.disabled = false;
      if (dialog.open && dialogBody.contains(control)) dialogBody.querySelector('.form-error').textContent = err.message;
      else toast(err.message);
      return;
    }
  }
  removeFromLibrary(id);
  toast(result?.cleanup_pending ? 'Episode removed. Local files are being cleaned up.' : 'Episode deleted.');
}
async function toggleSavedEpisode(control) {
  const id = control.dataset.id;
  control.disabled = true;
  const saved = control.getAttribute('aria-pressed') !== 'true';
  const result = await api(`/api/episodes/${idPart(id)}/save`, 'POST', {saved});

  // Discard snapshots requested before the write, including one already in flight on this detail page.
  stateRequest++;
  const episode = state.episodes.find(item => sameId(item.id, id));
  if (episode) episode.is_saved = result.is_saved;
  if (detail && sameId(detail.id, id)) {
    detailRequest++;
    detail.is_saved = result.is_saved;
  }
  lastState = JSON.stringify(state);
  if (route().page === 'saved') {
    renderSaved();
    const next = main.querySelector('.save-episode, .saved-empty a');
    if (next && document.activeElement === document.body) next.focus({preventScroll:true});
  } else {
    const current = main.querySelector(`button.save-episode[data-id="${CSS.escape(id)}"]`);
    if (current) {
      current.setAttribute('aria-pressed', String(result.is_saved));
      current.title = result.is_saved ? 'Remove from Saved' : 'Save for later';
      const label = current.querySelector('span');
      if (label) label.textContent = result.is_saved ? 'Saved' : 'Save';
      current.disabled = false;
    }
  }
  toast(result.is_saved ? 'Episode saved.' : 'Episode removed from Saved.');
}
function episodeRow(episode) {
  const feed = findFeed(episode.feed_id);
  const meta = [feed?.title || episode.feed_title, dateLabel(episode.published), episode.duration ? timeLabel(episode.duration) : ''].filter(Boolean).join(' · ');
  const view = route();
  const origin = view.page === 'saved' ? '/saved' : view.page === 'feed' ? '/feed/' + idPart(view.id) : '';
  const href = episodeLink(episode.id) + origin;
  return `<article class="episode-row">${cover(episode.title, episode.image_url || feed?.image_url, episode.id)}<div class="episode-text"><h3 id="episode-title-${escapeHtml(episode.id)}"><a href="${href}">${escapeHtml(episode.title)}</a></h3><p>${escapeHtml(meta)}</p>${playButton(episode, true)}</div>${statusPill(episode)}${saveButton(episode)}${deleteButton(episode)}<a class="icon-button" href="${href}" aria-label="Open episode">${icon('arrow')}</a></article>`;
}
function overview() {
  const {feeds, episodes, settings} = state;
  const busy = episodes.filter(e => ['processing', 'queued'].includes(e.status)).length;
  const failed = episodes.filter(e => e.status === 'failed').length;
  const processorLabel = busy ? countLabel(busy, 'episode') + ' in progress' : failed ? countLabel(failed, 'episode') + ' to review' : settings.processing_ready ? 'Ready for new episodes' : 'Setup required';
  const processorText = busy ? 'Open an episode to see its current stage.' : failed ? 'Open a failed episode to see the error and retry.' : settings.processing_ready ? `Feeds are checked every ${countLabel(settings.poll_minutes, 'minute')}.` : settings.setup_hint || 'Open server details to configure episode processing.';
  const subscribed = feeds.length ? `<div class="mini-feeds">${feeds.slice(0, 3).map(f => `<a class="mini-feed" href="${feedLink(f.id)}">${cover(f.title, f.image_url, f.id)}<span>${escapeHtml(f.title)}</span>${icon('arrow')}</a>`).join('')}</div>` : `<div class="info-item"><span class="tile-icon">${icon('rss')}</span><div><h3>No feeds yet</h3><p>Your podcast subscriptions will appear here.</p></div></div>`;
  return `<div class="overview"><section class="overview-section"><div class="section-title"><h2>Subscriptions <span class="count">${feeds.length}</span></h2><a class="link-button" href="#feeds">Manage ${icon('arrow')}</a></div>${subscribed}</section><section class="overview-section"><div class="section-title"><h2>Processing</h2>${button('server', 'Details', '', 'link-button')}</div><div class="info-item"><span class="tile-icon">${icon('sound')}</span><div><h3>${escapeHtml(processorLabel)}</h3><p>${escapeHtml(processorText)}</p></div></div></section></div>`;
}
function emptyHero() {
  return `<section class="hero"><div class="hero-copy"><span class="hero-symbol">${icon('rss')}</span><h2>Add your first podcast.</h2><p>Subscribe to a feed for audio with detected ads removed, a transcript, and notes on every new episode.</p>${button('subscribe', 'Add a feed', 'plus', 'button light')}</div><div class="hero-image"><img alt="" src="/static/cassette.jpg"></div></section>`;
}
function matchesFilter(episode) {
  return filter === 'all' || (filter === 'progress' ? ['processing','queued'].includes(episode.status) : episode.status === filter);
}
function listEpisodes(episodes, feedId = '') {
  const labels = {all:'All', ready:'Ready', progress:'In progress', failed:'Failed'};
  const tabs = Object.entries(labels).map(([value,label]) => `<button type="button" data-action="filter" data-value="${value}" aria-pressed="${filter === value}">${label}</button>`).join('');
  const feedSelect = state.feeds.length ? `<select id="feed-filter" aria-label="Filter by subscription"><option value="">All subscriptions</option>${state.feeds.map(f => `<option value="${escapeHtml(f.id)}" ${sameId(feedId,f.id) ? 'selected' : ''}>${escapeHtml(f.title)}</option>`).join('')}</select>` : '';
  const matching = episodes.filter(matchesFilter);
  const emptyMessage = filter === 'all' ? 'Waiting for a new episode from this feed.' : 'No episodes with this status.';
  return `<section aria-label="Episodes"><div class="filters"><div class="filter-tabs" aria-label="Filter by episode status">${tabs}</div>${feedSelect}</div>${matching.length ? matching.map(episodeRow).join('') : `<p class="list-empty">${emptyMessage}</p>`}</section>`;
}
function renderLibrary(feedId = '') {
  const feed = feedId && findFeed(feedId);
  const episodes = feed ? state.episodes.filter(e => sameId(e.feed_id, feed.id)) : state.episodes;
  const isEmpty = !state.feeds.length && !state.episodes.length;
  const title = feed ? feed.title : 'Listening room';
  document.title = `${title} — Aircut`;
  const meta = feed ? countLabel(episodes.length, 'episode') : `${countLabel(episodes.length, 'episode')} · ${countLabel(state.feeds.length, 'subscription')}`;
  const actions = isEmpty ? button('refresh', 'Refresh', 'refresh', 'quiet') : button('subscribe', 'Add feed', 'plus');
  main.innerHTML = `${feed ? `<a class="page-back" href="#library">${icon('back')} Library</a>` : ''}<div class="heading-row"><div><h1>${escapeHtml(title)}</h1><p>${meta}</p></div>${actions}</div>${isEmpty ? emptyHero() : listEpisodes(episodes, feedId)}${feed ? '' : overview()}`;
}
function renderSaved() {
  const episodes = state.episodes.filter(episode => episode.is_saved);
  document.title = 'Saved episodes — Aircut';
  const content = episodes.length
    ? episodes.map(episodeRow).join('')
    : `<div class="saved-empty"><span class="tile-icon">${icon('bookmark')}</span><p>Save an episode using its bookmark to listen later.</p><a class="button outline" href="#library">Browse library</a></div>`;
  main.innerHTML = `<div class="heading-row"><div><h1>Saved episodes</h1><p>${countLabel(episodes.length, 'episode')}</p></div></div>${content}`;
}
function renderFeeds() {
  const {feeds} = state;
  document.title = 'Subscriptions — Aircut';
  const rows = feeds.map(feed => {
    const subtitle = feed.last_error || (feed.last_checked ? `Last checked ${dateLabel(feed.last_checked)}` : 'Awaiting first check');
    return `<article class="feed-row">${cover(feed.title, feed.image_url, feed.id)}<div class="feed-main"><h3><a href="${feedLink(feed.id)}">${escapeHtml(feed.title)}</a></h3><p class="${feed.last_error ? 'warning' : ''}">${escapeHtml(subtitle)}</p></div><button type="button" class="quiet" data-action="remove-feed" data-id="${escapeHtml(feed.id)}">Unsubscribe</button></article>`;
  }).join('');
  main.innerHTML = `<div class="heading-row"><div><h1>Subscriptions</h1><p>${countLabel(feeds.length,'feed')} · Checking every ${countLabel(state.settings.poll_minutes,'minute')}</p></div>${button('subscribe','Add feed','plus')}</div>${feeds.length ? `<div class="feed-heading"><h2>Your feeds</h2>${button('refresh','Check feeds','refresh','quiet')}</div>${rows}` : `<section class="list-empty"><p>Your subscribed shows will appear here. Add a podcast’s RSS feed to start.</p></section>`}`;
}
let lastAdStats = '';
async function loadAdStats(request) {
  document.title = 'Ad stats — Aircut';
  if (!$('#stats-report')) main.innerHTML = '<p class="loading" role="status">Loading ad stats…</p>';
  try {
    const stats = await api('/api/ad-stats');
    if (request !== detailRequest) return;
    const fingerprint = JSON.stringify(stats);
    if ($('#stats-report') && fingerprint === lastAdStats) return;
    lastAdStats = fingerprint;
    const time = seconds => timeLabel(Math.round(seconds * 1000) / 1000 + 0.5);
    const seconds = Math.floor(Math.round(stats.removed_seconds * 1000) / 1000 + 0.5);
    const totalTime = [Math.floor(seconds / 3600) + 'h', Math.floor(seconds / 60) % 60 + 'm', seconds % 60 + 's'];
    const podcasts = stats.podcasts.map(show => {
      const percent = show.percent === null ? '—' : show.percent.toFixed(1) + '%';
      return `<tr><th scope="row">${escapeHtml(show.title)}</th><td>${show.episodes}</td><td>${time(show.removed_seconds)}</td>
        <td>${percent}${show.percent === null ? '' : `<span class="stats-track" aria-hidden="true"><i style="width:${show.percent}%"></i></span>`}</td></tr>`;
    });
    const advertisers = stats.advertisers.map(a => `<tr><th scope="row">${escapeHtml(a.name)}</th><td>${a.episodes}</td></tr>`);
    function ranking(id, title, explanation, columns, rows, empty) {
      const expanded = document.getElementById(id)?.classList.contains('expanded') || false;
      const more = rows.length > 10 ? `<button type="button" class="quiet" data-action="toggle-ranking" aria-controls="${id}-table" aria-expanded="${expanded}">${expanded ? 'Show fewer' : 'Show all ' + rows.length}</button>` : '';
      const table = `<table id="${id}-table" aria-labelledby="${id}-title"><thead><tr>${columns.map(c => `<th scope="col">${c}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
      return `<section class="stats-ranking${expanded ? ' expanded' : ''}" id="${id}"><h2 id="${id}-title">${title}</h2><p>${explanation}</p>${rows.length ? table + more : `<p>${empty}</p>`}</section>`;
    }
    const missing = stats.podcasts.some(s => s.missing_duration);
    main.innerHTML = `<div id="stats-report"><div class="heading-row"><div><h1>Ad stats</h1><p>Completed episodes in your library</p></div></div>
      <dl class="stats-totals"><div><dt>Total ad time removed</dt><dd>${totalTime.join(' ')}</dd></div>
      <div><dt>Episodes</dt><dd>${stats.episodes}</dd></div><div><dt>Podcasts</dt><dd>${stats.podcasts.length}</dd></div></dl>
      ${stats.episodes ? `<div class="stats-rankings">
        ${ranking('stats-podcasts','Ad share by podcast','Removed time as a share of original runtime, including episodes with no cuts.' + (missing ? ' Episodes with unknown runtime are excluded from percentages.' : ''),['Podcast','Episodes','Removed','Ad share'],podcasts)}
        ${ranking('stats-advertisers','Most frequent advertisers','Each identified advertiser counts once per episode, even across multiple breaks.',['Advertiser','Episodes'],advertisers,'No advertisers have been identified.')}
      </div>` : '<p class="list-empty">Stats will appear when Aircut finishes processing an episode.</p>'}</div>`;
  } catch (err) {
    if (request === detailRequest) showError(main, err.message);
  }
}

function renderRoute() {
  if (!state) return;
  const {page, id, fromSaved} = route();
  const savedPage = supportsSavedEpisodes() && (page === 'saved' || (page === 'episode' && fromSaved));
  $('#nav-saved').hidden = !supportsSavedEpisodes();
  $('#nav-saved').toggleAttribute('aria-current', savedPage);
  $('#nav-library').toggleAttribute('aria-current', !['feeds','rss','ad-stats'].includes(page) && !savedPage);
  $('#nav-feeds').toggleAttribute('aria-current', page === 'feeds');
  $('#nav-rss').toggleAttribute('aria-current', page === 'rss');
  $('#nav-stats').toggleAttribute('aria-current', page === 'ad-stats');
  for (const el of document.querySelectorAll('nav [aria-current]')) el.setAttribute('aria-current', 'page');
  if (page === 'episode' && id) {
    loadEpisode(id);
    return;
  }
  detailRequest++;
  detail = undefined;
  if (page === 'feeds') renderFeeds();
  else if (page === 'ad-stats') loadAdStats(detailRequest);
  else if (page === 'rss') renderRssFeeds();
  else if (savedPage) renderSaved();
  else renderLibrary(page === 'feed' ? id : '');
}
function prose(value) {
  // Only generated elements carry markup. Every line of model output remains escaped text.
  const inline = str => escapeHtml(str).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  let result = '', group = '', lines = [];
  const flush = () => {
    if (!lines.length) return;
    result += group === 'ul' || group === 'ol' ? `<${group}>${lines.map(line => `<li>${inline(line)}</li>`).join('')}</${group}>` : `<${group}>${inline(lines.join(' '))}</${group}>`;
    lines = [];
  };
  for (const raw of String(value || '').split('\n')) {
    const line = raw.trim();
    if (!line) { flush(); continue; }
    let type = 'p', content = line;
    const heading = line.match(/^#{1,6}\s+(.+)/), bullet = line.match(/^[-*•]\s+(.+)/), numbered = line.match(/^\d+[.)]\s+(.+)/);
    if (heading) { type = 'h3'; content = heading[1]; }
    else if (bullet) { type = 'ul'; content = bullet[1]; }
    else if (numbered) { type = 'ol'; content = numbered[1]; }
    if (group !== type || type === 'h3') flush();
    group = type;
    lines.push(content);
  }
  flush();
  return result;
}
async function loadEpisode(id) {
  const request = ++detailRequest;
  if (!detail || !sameId(detail.id, id)) {
    detail = undefined;
    main.innerHTML = '<p class="loading" role="status">Opening episode…</p>';
  }
  try {
    const episode = await api(`/api/episodes/${idPart(id)}`);
    if (request !== detailRequest) return;
    if (detail && sameId(detail.id, episode.id)) {
      detail.playback_revision = episode.playback_revision;
      detail.playback_position = episode.playback_position;
    }
    if (JSON.stringify(detail) !== JSON.stringify(episode)) {
      detail = episode;
      renderEpisode();
    }
    updatePlayButton(id);
  } catch (err) {
    if (request === detailRequest) {
      detail = undefined;
      if (err.status === 404) {
        removeFromLibrary(id);
        toast('This episode is no longer in Aircut.');
      } else showError(main, err.message);
    }
  }
}
function renderEpisode() {
  const e = detail, feed = findFeed(e.feed_id);
  document.title = `${e.title} — Aircut`;
  const meta = [feed?.title || e.feed_title, dateLabel(e.published), e.duration ? timeLabel(e.duration) : ''].filter(Boolean).join(' · ');
  const play = playButton(e);
  const retry = e.status === 'failed' ? button('retry', 'Retry episode', 'refresh', 'button outline') : '';
  const stage = e.status === 'processing' || e.status === 'queued' ? `<p class="setup-note" role="status">${escapeHtml(e.stage || 'Waiting to process')}${!state.settings.processing_ready ? ` · ${escapeHtml(state.settings.setup_hint || 'Processing setup required')}` : ''}</p>` : '';
  const sections = [];
  if (e.briefing_html) {
    sections.push(`<article class="notes prose briefing">${e.briefing_html}</article>`);
  } else {
    if (e.takeaways) sections.push(`<section class="notes"><h2>Main takeaways</h2><div class="prose">${prose(e.takeaways)}</div></section>`);
    if (e.summary) sections.push(`<section class="notes"><h2>Comprehensive summary</h2><div class="prose">${prose(e.summary)}</div></section>`);
  }
  if (e.transcript) sections.push(`<details class="notes"><summary>Transcript</summary><p class="transcript">${escapeHtml(e.transcript)}</p></details>`);
  if (e.removed_ads_html) sections.push(e.removed_ads_html);
  const view = route(), back = episodeListLink(view);
  const backLabel = back === '#saved' ? 'Saved episodes' : findFeed(view.fromFeed)?.title || 'Library';
  main.innerHTML = `<a href="${back}" class="page-back">${icon('back')} ${escapeHtml(backLabel)}</a><div class="detail-head">${cover(e.title, e.image_url || feed?.image_url, e.id)}<div class="episode-heading"><p class="detail-meta">${escapeHtml(meta)}</p><h1>${escapeHtml(e.title)}</h1><div class="detail-actions">${play}${saveButton(e, true)}${statusPill(e)}${retry}${deleteButton(e, true)}</div>${stage}</div></div><div class="detail-body">${e.error ? `<p class="form-error" role="alert">${escapeHtml(e.error)}</p>` : ''}${e.description ? `<details class="notes"><summary>Episode description</summary><p class="transcript">${escapeHtml(e.description)}</p></details>` : ''}${sections.join('')}</div>`;
}
function openDialog(kind, glyph, content) {
  currentDialog = kind;
  dialog.removeAttribute('aria-describedby');
  $('#dialog-mark').innerHTML = icon(glyph);
  dialogBody.innerHTML = content;
  if (!dialog.open) dialog.showModal();
}
dialog.addEventListener('close', () => { currentDialog = ''; deviceSignature = ''; });
dialog.addEventListener('click', event => {
  if (event.target !== dialog) return;
  const box = dialog.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
});
function openSubscribe() {
  openDialog('subscribe', 'rss', `<h2 id="dialog-title">Add a feed</h2><p>Subscribe with a podcast’s RSS feed URL. Aircut will process new episodes as they arrive.</p><form id="subscribe-form"><label for="feed-url">Podcast feed URL</label><input required type="url" name="url" id="feed-url" placeholder="https://example.com/podcast.xml" inputmode="url" autocapitalize="none" spellcheck="false" autocomplete="off"><label for="backfill">Process existing episodes</label><select name="backfill" id="backfill"><option value="0">New episodes only</option><option value="1" selected>Latest episode</option><option value="3">Latest 3 episodes</option><option value="5">Latest 5 episodes</option><option value="10">Latest 10 episodes</option></select><div class="form-error" role="alert"></div><button type="submit" class="button">${icon('plus')}Subscribe</button></form>`);
  $('#feed-url').focus();
}
function openRemove(id) {
  const feed = findFeed(id);
  if (!feed) return;
  dialogFeed = id;
  openDialog('remove', 'rss', `<h2 id="dialog-title">Unsubscribe?</h2><p>Stop checking <strong>${escapeHtml(feed.title)}</strong> and cancel pending work for this feed. Completed episodes stay in your library.</p><div class="form-error" role="alert"></div><div class="action-list">${button('confirm-remove', 'Unsubscribe', '', 'button danger')}${button('close-dialog', 'Cancel', '', 'button outline')}</div>`);
}
function renderServer() {
  if (!state) return;
  const s = state.settings;
  const params = [ ['Episode processing', s.processing_ready ? 'Ready' : 'Setup required'], ['Feed checks', `Every ${countLabel(s.poll_minutes,'minute')}`], ['Subscriptions', state.feeds.length], ['iPhone access', s.remote_url ? 'Enabled' : 'Local only'] ];
  const extra = s.setup_hint ? `<p class="setup-box">${escapeHtml(s.setup_hint)}</p>` : '';
  openDialog('server','settings',`<h2 id="dialog-title">Server details</h2>${params.map(([key,value]) => `<div class="setting-line"><strong>${escapeHtml(key)}</strong><span>${escapeHtml(value)}</span></div>`).join('')}${extra}<div class="action-list">${playerSettingsSupported() ? button('player-settings', 'Playback settings', 'settings', 'button outline') : ''}${button('refresh', 'Check feeds now', 'refresh', 'button outline')}${button('device', 'iPhone access', 'phone', 'button outline')}</div>`);
}
function linkBlock(title, url, how, key) {
  return `<div class="link-block"><label for="${key}-url">${escapeHtml(title)}</label>${how ? `<p>${escapeHtml(how)}</p>` : ''}<div class="link-input"><input type="text" readonly id="${key}-url" value="${escapeHtml(url)}" spellcheck="false"><button type="button" class="button outline" data-action="copy" data-key="${key}" aria-label="Copy ${escapeHtml(title)}">${icon('copy')} Copy</button></div></div>`;
}
function renderRssFeeds() {
  document.title = 'RSS feeds — Aircut';
  const {settings, feeds} = state;
  const hint = settings.remote_url ? 'Paste a private feed URL into your podcast app.' : 'These links work on this computer. Enable iPhone access to use them on another device.';
  const combined = linkBlock('All podcasts', settings.rss_url, '', 'combined');
  const shows = feeds.map(feed => linkBlock(feed.title, feed.rss_url, '', `show-${feed.id}`)).join('');
  main.innerHTML = `<div class="heading-row"><div><h1>RSS feeds</h1><p>${hint}</p></div></div>
    <div class="action-list">${button('copy-feeds', 'Copy all URLs', 'copy')}<a class="button outline" href="/feeds.txt">Open feeds.txt</a></div>
    <div class="rss-directory">${combined}${shows}</div>`;
}
function renderDevice() {
  if (!state) return;
  const s = state.settings;
  const remote = httpUrl(s.remote_url), rss = httpUrl(s.rss_url);
  if (remote || s.tunnel_error) waitingTunnel = false;
  const starting = waitingTunnel || s.tunnel_starting;
  const signature = JSON.stringify([remote, rss, starting, s.can_start_tunnel, s.tunnel_error, s.local, s.temporary_tunnel]);
  if (currentDialog === 'device' && signature === deviceSignature) return;
  deviceSignature = signature;
  let body = '<h2 id="dialog-title">iPhone access</h2>';
  if (remote) {
    body += '<p>Keep the server computer awake while streaming. Treat your private links like passwords.</p>';
    if (s.local) body += linkBlock('Safari pairing link', remote, 'Open this link on your iPhone to use the listening room.', 'safari');
    if (rss) body += linkBlock('Private podcast feed', rss, 'In Apple Podcasts, open Library, tap the More menu, choose Follow a Show by URL, and paste this link.', 'rss');
    if (s.temporary_tunnel) body += '<p class="setup-box">Restarting this temporary connection changes its address. Pair Safari again and follow the new feed URL in Apple Podcasts.</p>';
    if (s.local && s.temporary_tunnel) body += button('request-stop-tunnel', 'Disable iPhone access', '', 'button outline');
  } else {
    body += '<p>The listening room is currently local to this computer.</p>';
    if (s.can_start_tunnel) {
      body += '<p class="setup-box">Enable a temporary HTTPS connection to listen in Safari or follow your private feed in Apple Podcasts. Keep this computer awake while streaming.</p>';
      if (starting) body += '<p role="status" class="setup-note">Opening a secure connection…</p>';
      else body += button('start-tunnel', 'Enable iPhone access', 'phone');
    } else body += '<p class="setup-box">Configure a reachable HTTPS address on the server to listen away from this computer.</p>';
    if (s.tunnel_error) body += `<p class="form-error" role="alert">${escapeHtml(s.tunnel_error)}</p>`;
    if (rss) body += linkBlock('Local podcast feed', rss, 'This address is for playback on the server computer.', 'rss');
  }
  if (s.local === false) body += button('logout', 'Sign out of this device', '', 'button outline');
  body += '<div id="device-feedback" class="form-error" role="status"></div>';
  openDialog('device', 'phone', body);
}
async function copyLink(key) {
  const input = document.getElementById(`${key}-url`);
  if (!input) return;
  const feedback = input.closest('dialog') && $('#device-feedback');
  let copied = false;
  try { await navigator.clipboard.writeText(input.value); copied = true; }
  catch {
    input.focus(); input.select(); input.setSelectionRange(0, input.value.length);
    try { copied = document.execCommand('copy'); } catch { /* Manual copy is available in the input. */ }
  }
  const message = copied ? 'Link copied.' : 'Select and copy the link.';
  if (feedback) feedback.textContent = message;
  else toast(message);
}
async function subscribe(form) {
  const url = form.elements.url.value.trim();
  const error = form.querySelector('.form-error');
  if (!/^https?:\/\//i.test(url)) { error.textContent = 'Enter an HTTP or HTTPS podcast feed URL.'; return; }
  const submit = form.querySelector('[type=submit]');
  submit.disabled = true;
  submit.textContent = 'Subscribing…';
  error.textContent = '';
  try {
    await api('/api/feeds','POST',{url,backfill:Number(form.elements.backfill.value)});
    if (dialog.open && currentDialog === 'subscribe') dialog.close();
    await loadState(true);
    toast('Podcast feed added.');
  } catch (err) { error.textContent = err.message; }
  finally { submit.disabled = false; submit.textContent = 'Subscribe'; }
}
async function refreshFeeds() {
  if (refreshing) return;
  refreshing = true;
  document.querySelectorAll('[data-action=refresh]').forEach(b => { b.disabled = true; });
  try {
    await api('/api/refresh','POST',{});
    await loadState(true);
    toast('Feed check requested.');
    if (currentDialog === 'server') renderServer();
  } catch (err) { toast(err.message); }
  finally { refreshing = false; document.querySelectorAll('[data-action=refresh]').forEach(b => { b.disabled = false; }); }
}
async function startTunnel() {
  if (waitingTunnel) return;
  waitingTunnel = true;
  deviceSignature = '';
  // A previous error may still be present until the server accepts the new request.
  state.settings.tunnel_error = '';
  renderDevice();
  try {
    await api('/api/tunnel','POST',{});
    await loadState();
  } catch (err) {
    waitingTunnel = false;
    if (currentDialog === 'device') { renderDevice(); $('#device-feedback').textContent = err.message; }
    else toast(err.message);
  }
}
document.addEventListener('submit', event => {
  if (event.target.id === 'player-settings-form') {
    event.preventDefault(); savePlayerSettings(event.target); return;
  }
  if (event.target.id !== 'subscribe-form') return;
  event.preventDefault(); subscribe(event.target);
});
document.addEventListener('change', event => {
  if (event.target.id !== 'feed-filter') return;
  location.hash = event.target.value ? feedLink(event.target.value) : '#library';
});
document.addEventListener('click', async event => {
  const control = event.target.closest('[data-action]');
  if (!control || control.disabled) return;
  const action = control.dataset.action;
  if (!state && !['reload','close-dialog'].includes(action)) { toast('Waiting for the server.'); return; }
  try {
    switch (action) {
      case 'toggle-ranking': {
        const table = document.getElementById(control.getAttribute('aria-controls'));
        const expanded = table.closest('.stats-ranking').classList.toggle('expanded');
        control.setAttribute('aria-expanded', expanded);
        control.textContent = expanded ? 'Show fewer' : 'Show all ' + table.tBodies[0].rows.length;
        break;
      }
      case 'reload': await loadState(true); break;
      case 'subscribe': openSubscribe(); break;
      case 'save-episode': await toggleSavedEpisode(control); break;
      case 'delete-episode': openEpisodeDelete(control.dataset.id); break;
      case 'confirm-delete-episode': await confirmEpisodeDelete(control); break;
      case 'close-dialog': dialog.close(); break;
      case 'server': renderServer(); break;
      case 'device': renderDevice(); break;
      case 'copy': await copyLink(control.dataset.key); break;
      case 'copy-feeds':
        try {
          const urls = Array.from(main.querySelectorAll('.rss-directory input'), input => input.value);
          await navigator.clipboard.writeText(urls.join('\n') + '\n');
          toast('Feed URLs copied.');
        } catch { toast('Open feeds.txt to select and copy all URLs.'); }
        break;
      case 'start-tunnel': await startTunnel(); break;
      case 'request-stop-tunnel':
        openDialog('stop-tunnel', 'phone', `<h2 id="dialog-title">Disable iPhone access?</h2><p>This closes the temporary connection and stops remote playback. Enabling it again creates new links.</p><div class="form-error" role="alert"></div><div class="action-list">${button('stop-tunnel', 'Disable access', '', 'button danger')}${button('device', 'Cancel', '', 'button outline')}</div>`);
        break;
      case 'stop-tunnel':
        control.disabled = true;
        await api('/api/tunnel', 'DELETE');
        await loadState(true); renderDevice(); break;
      case 'logout':
        control.disabled = true;
        await api('/api/logout', 'POST', {});
        location.replace('/'); break;
      case 'remove-feed': openRemove(control.dataset.id); break;
      case 'confirm-remove':
        control.disabled = true;
        await api(`/api/feeds/${idPart(dialogFeed)}`, 'DELETE');
        dialog.close(); await loadState(true); toast('Unsubscribed.'); break;
      case 'filter': filter = control.dataset.value; renderRoute(); break;
      case 'refresh': await refreshFeeds(); break;
      case 'play': playEpisode(episodeForPlayback(control.dataset.id)); break;
      case 'skip-back': skipBy(-skipIntervals().back); break;
      case 'skip-forward': skipBy(skipIntervals().forward); break;
      case 'player-settings': openPlayerSettings(); break;
      case 'close-player': closePlayer(); break;
      case 'show-playing': if (playing) location.hash = episodeLink(playing.id); break;
      case 'retry':
        if (!detail) break;
        control.disabled = true;
        await api(`/api/episodes/${idPart(detail.id)}/retry`, 'POST', {});
        await loadState(true); toast('Episode queued for retry.'); break;
    }
  } catch (err) {
    control.disabled = false;
    const field = dialog.open && dialogBody.querySelector('.form-error');
    if (field) field.textContent = err.message;
    else toast(err.message);
  }
});
$('.skip').addEventListener('click', event => {
  event.preventDefault();
  main.focus();
});
window.addEventListener('hashchange', () => {
  if (dialog.open) dialog.close();
  filter = 'all'; renderRoute(); window.scrollTo(0, 0);
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadState(); });
setInterval(() => { if (!document.hidden) loadState(); }, 6500);
window.addEventListener('DOMContentLoaded', () => loadState(), {once:true});
