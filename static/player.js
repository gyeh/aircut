'use strict';

let playing;
let nextCheckpoint = 0;
const playheads = new Map();

function playerSettingsSupported() {
  return Number.isInteger(state?.settings.skip_back_seconds);
}

function skipIntervals() {
  return {
    back: state?.settings.skip_back_seconds ?? 15,
    forward: state?.settings.skip_forward_seconds ?? 30,
  };
}

function updatePlayerControls() {
  const intervals = skipIntervals();
  for (const direction of ['back', 'forward']) {
    const control = document.querySelector(`[data-action="skip-${direction}"]`);
    const label = `Skip ${direction} ${countLabel(intervals[direction], 'second')}`;
    control.setAttribute('aria-label', label);
    control.title = label;
    control.disabled = !playing?.ready;
    $(`#skip-${direction}-label`).textContent = intervals[direction] + 's';
  }
  $('#player [data-action="player-settings"]').hidden = !playerSettingsSupported();
}

function openPlayerSettings() {
  const {back, forward} = skipIntervals();
  openDialog('playback', 'settings', `<h2 id="dialog-title">Playback settings</h2>
    <form id="player-settings-form">
      <label for="back-seconds">Skip back (seconds)</label>
      <input type="number" name="back" id="back-seconds" min="1" max="300" step="1" inputmode="numeric" required value="${back}">
      <label for="forward-seconds">Skip forward (seconds)</label>
      <input type="number" name="forward" id="forward-seconds" min="1" max="300" step="1" inputmode="numeric" required value="${forward}">
      <p class="form-error" role="alert"></p><button type="submit" class="button">Save settings</button>
    </form>`);
}

async function savePlayerSettings(form) {
  const submit = form.querySelector('[type=submit]');
  submit.disabled = true;
  try {
    const result = await api('/api/player/settings', 'POST', {
      skip_back_seconds:form.elements.back.valueAsNumber,
      skip_forward_seconds:form.elements.forward.valueAsNumber,
    });
    Object.assign(state.settings, result);
    updatePlayerControls();
    if (currentDialog === 'playback') dialog.close();
    toast('Playback settings saved.');
  } catch (err) {
    form.querySelector('.form-error').textContent = err.message;
  } finally {
    submit.disabled = false;
  }
}

// Keep a small unsent checkpoint through a reload or a dropped connection. A revision
// check prevents a stale local checkpoint from replacing newer progress on another device.
function storedCheckpoint(id) {
  try { return JSON.parse(localStorage.getItem('aircut-progress-' + id)); }
  catch { return null; }
}

function storeCheckpoint(head) {
  try {
    const key = 'aircut-progress-' + head.id;
    if (head.pending !== null) {
      localStorage.setItem(key, JSON.stringify({
        revision:head.revision, position:head.pending, inFlight:head.inFlight,
      }));
    } else if (storedCheckpoint(head.id)?.revision <= head.revision) {
      localStorage.removeItem(key);
    }
  } catch { /* The server can still save progress if browser storage is unavailable. */ }
}

function playheadFor(episode) {
  const revision = episode.playback_revision ?? 0;
  const key = String(episode.id);
  let head = playheads.get(key);
  if (!head) {
    head = {id:episode.id, position:episode.playback_position || 0, revision, pending:null, inFlight:null};
    const saved = storedCheckpoint(episode.id);
    // If navigation interrupted the reply to a successful write, our next local
    // checkpoint can still follow it, provided the server acknowledged that timestamp.
    if (saved && Number.isFinite(saved.position) && saved.position >= 0 &&
        (saved.revision === revision || (saved.revision + 1 === revision && saved.inFlight === head.position))) {
      head.pending = saved.position;
    }
    playheads.set(key, head);
  } else if (head.pending === null && head.inFlight === null && revision > head.revision) {
    head.position = episode.playback_position || 0;
    head.revision = revision;
  }
  return head;
}

function reflectProgress(head) {
  const episode = state?.episodes.find(e => sameId(e.id, head.id));
  for (const item of [episode, detail]) {
    if (item && sameId(item.id, head.id) && (item.playback_revision ?? 0) <= head.revision) {
      item.playback_position = head.position;
      item.playback_revision = head.revision;
    }
  }
  updatePlayButton(head.id);
}

async function flushProgress(head) {
  if (head.inFlight !== null || head.pending === null) return;
  const target = head.pending;
  head.inFlight = target;
  storeCheckpoint(head);
  try {
    const response = await fetch(`/api/episodes/${idPart(head.id)}/progress`, {
      method:'POST', keepalive:true, headers:{'Content-Type':'application/json'},
      body:JSON.stringify({position:target, revision:head.revision}),
    });
    const result = await response.json();
    if (playheads.get(String(head.id)) !== head) return;
    if (response.status !== 409 && !response.ok) throw new Error(result.error);
    if (!Number.isFinite(result.playback_position) || !Number.isInteger(result.playback_revision)) {
      throw new Error('Invalid playback checkpoint');
    }
    head.revision = result.playback_revision;
    head.position = result.playback_position;
    head.inFlight = null;
    if (response.status === 409) {
      head.pending = null;
      if (playing?.head === head) {
        playing.awaitingResume = true;
        audio.pause();
        setPlayPosition(head.position);
        toast('Playback changed on another device. Resume here to take over.');
      }
    } else if (head.pending === target) {
      head.pending = null;
    }
    head.warned = false;
    storeCheckpoint(head);
    reflectProgress(head);
    if (head.pending !== null) flushProgress(head);
  } catch {
    if (playheads.get(String(head.id)) !== head) return;
    head.inFlight = null;
    storeCheckpoint(head);
    if (!head.warned) toast('Listening position could not sync. Aircut will try again.');
    head.warned = true;
  }
}

function checkpoint() {
  if (!playerSettingsSupported() || !playing?.ready || playing.awaitingResume) return;
  const head = playing.head;
  const value = audio.ended ? 0 : audio.currentTime;
  if (!Number.isFinite(value)) return;
  const position = Math.round(value * 100) / 100;
  if (position !== (head.pending ?? head.position)) {
    head.pending = position;
    storeCheckpoint(head);
  }
  flushProgress(head);
}

function setPlayPosition(position) {
  const end = Number.isFinite(audio.duration) ? audio.duration : Infinity;
  const target = Math.min(end, Math.max(0, position));
  if (Math.abs(target - audio.currentTime) > 0.01) audio.currentTime = target;
}

function skipBy(seconds) {
  if (playing?.ready) setPlayPosition(audio.currentTime + seconds);
}

function episodeForPlayback(id) {
  if (detail && sameId(detail.id, id)) return detail;
  return state?.episodes.find(episode => sameId(episode.id, id));
}

function episodePlayLabel(episode, inList) {
  if (playing && sameId(playing.id, episode.id) && !audio.paused) return inList ? 'Pause' : 'Pause episode';
  const head = playheads.get(String(episode.id));
  const position = head && (head.pending !== null || head.revision >= (episode.playback_revision ?? 0))
    ? head.pending ?? head.position : episode.playback_position;
  if (position >= 1) return 'Resume at ' + timeLabel(position);
  return inList ? 'Play' : 'Play episode';
}

function playButtonContent(label) {
  return icon(label.startsWith('Pause') ? 'pause' : 'play') + escapeHtml(label);
}

function playButton(episode, inList = false) {
  if (!episode.has_audio) return '';
  const id = escapeHtml(episode.id);
  const description = inList ? ` aria-describedby="episode-title-${id}"` : '';
  const style = inList ? 'row-play button outline' : 'button';
  return `<button type="button" class="${style}" data-action="play" data-id="${id}"${description}>${playButtonContent(episodePlayLabel(episode, inList))}</button>`;
}

function playControl(id) {
  return main.querySelector(`[data-action="play"][data-id="${CSS.escape(String(id))}"]`);
}

function updatePlayButton(id = playing?.id) {
  if (id == null) return;
  const control = playControl(id);
  if (!control) return;
  const episode = episodeForPlayback(id);
  if (episode) {
    const label = episodePlayLabel(episode, control.classList.contains('row-play'));
    if (control.textContent !== label) control.innerHTML = playButtonContent(label);
  }
}

function playEpisode(episode) {
  if (!episode?.has_audio) return;
  if (playing && sameId(playing.id, episode.id) && !audio.paused) {
    audio.pause();
    return;
  }
  const url = httpUrl(episode.audio_url ?? `/audio/${idPart(episode.id)}.mp3`, true);
  if (!url) { toast('Audio is unavailable.'); return; }
  if (!playing || !sameId(playing.id, episode.id)) {
    const previous = playing;
    checkpoint();
    const head = playheadFor(episode);
    playing = {id:episode.id, head, ready:false};
    audio.src = url;
    if (previous) updatePlayButton(previous.id);
    $('#player-title').textContent = episode.title;
    if ('mediaSession' in navigator && 'MediaMetadata' in window) {
      navigator.mediaSession.metadata = new MediaMetadata({title:episode.title,artist:episode.feed_title || 'Aircut',album:'Aircut'});
    }
  }
  $('#player').hidden = false;
  // Invoke play within the tap gesture for iOS. loadedmetadata restores the
  // playhead before the first saved checkpoint.
  const session = playing;
  audio.play().catch(err => {
    if (playing === session && err.name !== 'AbortError') toast('Tap play in the audio controls to start listening.');
  });
  updatePlayerControls();
}

audio.addEventListener('loadedmetadata', () => {
  if (!playing) return;
  setPlayPosition(playing.head.pending ?? playing.head.position);
  playing.ready = true;
  nextCheckpoint = performance.now() + 8000;
  updatePlayerControls();
  if (playerSettingsSupported()) flushProgress(playing.head);
});
audio.addEventListener('timeupdate', () => {
  if (audio.paused || performance.now() < nextCheckpoint) return;
  nextCheckpoint = performance.now() + 8000;
  checkpoint();
});
audio.addEventListener('play', () => {
  if (playing) playing.awaitingResume = false;
  if (playing?.ready && playing.head.pending === null && playing.head.inFlight === null) {
    let latest = state?.episodes.find(e => sameId(e.id, playing.id));
    if (detail && sameId(detail.id, playing.id) && detail.playback_revision > (latest?.playback_revision ?? -1)) {
      latest = detail;
    }
    if (latest && latest.playback_revision > playing.head.revision) {
      playheadFor(latest);
      setPlayPosition(playing.head.position);
    }
  }
  updatePlayButton();
});
audio.addEventListener('pause', () => { if (audio.paused) checkpoint(); updatePlayButton(); });
audio.addEventListener('seeked', checkpoint);
audio.addEventListener('ended', () => { checkpoint(); updatePlayButton(); });
audio.addEventListener('error', () => { if (playing) toast('Audio could not load. Check the connection and reopen the episode.'); });
window.addEventListener('pagehide', checkpoint);
window.addEventListener('online', () => {
  if (playerSettingsSupported()) playheads.forEach(flushProgress);
});
document.addEventListener('visibilitychange', () => { if (document.hidden) checkpoint(); });

if ('mediaSession' in navigator) {
  for (const [action, handler] of [
    ['seekbackward', () => skipBy(-skipIntervals().back)],
    ['seekforward', () => skipBy(skipIntervals().forward)],
  ]) {
    try { navigator.mediaSession.setActionHandler(action, handler); }
    catch { /* Available lock-screen actions vary by browser. */ }
  }
}

function discardEpisodePlayback(id) {
  if (playing && sameId(playing.id, id)) closePlayer(false);
  playheads.delete(String(id));
  try { localStorage.removeItem('aircut-progress-' + id); }
  catch { /* Browser storage can be unavailable. */ }
}

function closePlayer(savePosition = true) {
  const previous = playing;
  if (savePosition) checkpoint();
  playing = undefined;
  audio.pause(); audio.removeAttribute('src'); audio.load();
  $('#player').hidden = true;
  if ('mediaSession' in navigator) navigator.mediaSession.metadata = null;
  if (previous) updatePlayButton(previous.id);
}
