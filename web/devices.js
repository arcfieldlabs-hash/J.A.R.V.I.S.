/* Local, user-controlled media capture. All devices start off on every page load. */
(function () {
  'use strict';

  function mount(options) {
    const {container, enabled, api, addMessage, onTranscript, onAnalysis} = options;
    if (!container) throw new Error('A device panel container is required.');
    const local = Boolean(enabled);
    const sources = ['camera', 'microphone', 'screen'];
    const generations = {camera: 0, microphone: 0, screen: 0};
    const accessQueues = {camera: Promise.resolve(), microphone: Promise.resolve(), screen: Promise.resolve()};
    const jobs = new Set();
    const camera = {stream: null, requesting: false, busy: false};
    const microphone = {stream: null, requesting: false, busy: false, recording: null};
    const screens = new Map();
    let screenRequests = 0, screenNumber = 0, speaking = false, disposed = false;

    function node(tag, text, parent) {
      const element = document.createElement(tag);
      if (text != null) element.textContent = text;
      if (parent) parent.append(element);
      return element;
    }
    function button(text, parent, handler) {
      const element = node('button', text, parent);
      element.type = 'button';
      element.style.cssText = 'border:1px solid #2f81a4;border-radius:7px;background:#164961;color:#d8f5ff;padding:8px 11px';
      element.addEventListener('click', handler);
      return element;
    }
    function row(parent) {
      const element = node('div', null, parent);
      element.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0';
      return element;
    }
    function preview(parent, label) {
      const element = node('video', null, parent);
      element.autoplay = true;
      element.muted = true;
      element.playsInline = true;
      element.setAttribute('aria-label', label);
      element.style.cssText = 'display:block;width:100%;max-height:180px;object-fit:contain;border-radius:8px;background:#030810';
      return element;
    }
    function status(text) { statusElement.textContent = text; }
    function errorText(error) {
      if (error && (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError')) {
        return 'Permission was declined. You can enable it again whenever you choose. Check browser and macOS Privacy & Security permissions if it keeps being denied.';
      }
      if (error && error.name === 'NotFoundError') return 'No matching device was found. Connect it and try again.';
      if (error && error.name === 'NotReadableError') return 'The device could not be opened. Check whether another app is using it.';
      if (error && error.name === 'AbortError') return 'The device request was cancelled.';
      return error && error.message ? error.message : 'The device request failed.';
    }
    function report(error) { status(errorText(error)); }
    function stopTracks(stream) { if (stream) stream.getTracks().forEach(track => track.stop()); }
    function post(path, payload, extra) {
      return api(path, {...extra, method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    }
    function setAccess(source, state) {
      // A late permission response cannot overtake the user's subsequent OFF request.
      const next = accessQueues[source].catch(() => {}).then(() => post('/api/devices', {source, enabled: state}, {keepalive: !state}));
      accessQueues[source] = next;
      return next;
    }
    function revoke(source) {
      generations[source]++;
      for (const job of jobs) if (job.source === source) job.controller.abort();
      return setAccess(source, false).catch(error => {
        if (!disposed) status('Capture stopped. Could not notify the local server: ' + errorText(error));
      });
    }
    function isCurrent(source, generation) { return !disposed && generations[source] === generation; }
    function delay(ms, signal) {
      return new Promise((resolve, reject) => {
        if (signal.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
        const cancel = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
        const timer = setTimeout(() => { signal.removeEventListener('abort', cancel); resolve(); }, ms);
        signal.addEventListener('abort', cancel, {once: true});
      });
    }
    async function mediaJob(source, path, payload, valid) {
      const generation = generations[source];
      const job = {source, controller: new AbortController()};
      const current = () => isCurrent(source, generation) && valid() && !job.controller.signal.aborted;
      jobs.add(job);
      try {
        if (!current()) return null;
        const queued = await post(path, payload, {signal: job.controller.signal});
        if (!current()) return null;
        if (!queued.request_id) throw new Error('The local server did not return a media request ID.');
        const deadline = Date.now() + 180000;
        while (current()) {
          if (Date.now() > deadline) throw new Error('This media request took too long. Check the local server before trying again.');
          await delay(500, job.controller.signal);
          const result = await api('/api/media/' + encodeURIComponent(queued.request_id), {signal: job.controller.signal});
          if (!current()) return null;
          if (result.status === 'pending' || result.status === 'running') continue;
          if (result.status === 'error' || result.status === 'cancelled') throw new Error(result.error || 'The media request was cancelled.');
          return result;
        }
        return null;
      } catch (error) {
        if (current()) throw error;
        return null;
      } finally { jobs.delete(job); }
    }

    container.style.cssText = 'margin-top:18px;border:1px solid var(--line,#163248);border-radius:10px;padding:16px;background:#08131fbb';
    const title = node('h2', 'Camera, microphone & screens', container);
    title.style.cssText = 'font-size:14px;font-weight:500;margin:0 0 8px';
    const guidance = node('p', local
      ? 'All devices start off. Video previews stay in this browser. Only “Ask about” sends one picture; “Record request” sends up to 10 seconds of audio to your local Jarvis server.'
      : 'Open local Jarvis on your Mac to use camera, microphone, and screen controls.', container);
    guidance.className = 'detail';
    const controls = row(container);
    const cameraButton = button('Camera: off', controls, () => {
      if (camera.stream || camera.requesting) stopCamera(); else enableCamera();
    });
    const microphoneButton = button('Microphone: off', controls, () => {
      if (microphone.stream || microphone.requesting) stopMicrophone(); else enableMicrophone();
    });
    const screenButton = button('Share screen', controls, addScreen);
    const stopButton = button('Stop all', controls, () => { stopAll(); status('All capture stopped.'); });
    const microphoneControls = row(container);
    const recordButton = button('Record request', microphoneControls, () => {
      if (microphone.recording) finishRecording(true); else startRecording();
    });
    const microphoneNote = node('span', 'Microphone is off.', microphoneControls);
    microphoneNote.className = 'detail';
    const cameraBox = node('div', null, container);
    cameraBox.hidden = true;
    const cameraVideo = preview(cameraBox, 'Local camera preview');
    const cameraQuestion = row(cameraBox);
    const cameraPrompt = node('input', null, cameraQuestion);
    cameraPrompt.type = 'text';
    cameraPrompt.placeholder = 'What would you like to ask about this view?';
    cameraPrompt.setAttribute('aria-label', 'Question about camera');
    cameraPrompt.maxLength = 2000;
    cameraPrompt.style.flex = '1 1 170px';
    const cameraAsk = button('Ask about camera', cameraQuestion, () => askAbout('camera', camera, cameraVideo, cameraPrompt, cameraAsk));
    const screensBox = node('div', null, container);
    const statusElement = node('p', local ? 'Camera, microphone, and screen sharing are off.' : 'Media capture is unavailable in the hosted preview.', container);
    statusElement.className = 'detail';
    statusElement.setAttribute('role', 'status');
    statusElement.setAttribute('aria-live', 'polite');

    function refresh() {
      cameraButton.disabled = microphoneButton.disabled = screenButton.disabled = !local || disposed;
      cameraButton.textContent = camera.requesting ? 'Cancel camera request' : camera.stream ? 'Camera: on · turn off' : 'Camera: off';
      cameraButton.setAttribute('aria-pressed', String(Boolean(camera.stream || camera.requesting)));
      microphoneButton.textContent = microphone.requesting ? 'Cancel microphone request' : microphone.stream ? 'Microphone: on · turn off' : 'Microphone: off';
      microphoneButton.setAttribute('aria-pressed', String(Boolean(microphone.stream || microphone.requesting)));
      screenButton.textContent = screens.size ? 'Add screen' : screenRequests ? 'Choose a screen…' : 'Share screen';
      screenButton.disabled = !local || disposed || screenRequests > 0;
      stopButton.disabled = !local || disposed || !(camera.stream || camera.requesting || microphone.stream || microphone.requesting || screens.size || screenRequests);
      recordButton.disabled = !local || disposed || !microphone.stream || microphone.busy || speaking;
      recordButton.textContent = microphone.recording ? 'Stop recording & send' : microphone.busy ? 'Transcribing…' : 'Record request';
      microphoneNote.textContent = !microphone.stream ? 'Microphone is off.' : speaking ? 'Paused while Jarvis speaks.' : microphone.recording ? 'Recording · stops after 10 seconds.' : microphone.busy ? 'Waiting for transcription.' : 'Ready; records only when you press Record request.';
      cameraAsk.disabled = !local || disposed || !camera.stream || camera.busy;
      cameraAsk.textContent = camera.busy ? 'Analysing…' : 'Ask about camera';
      for (const screen of screens.values()) {
        screen.ask.disabled = disposed || screen.busy;
        screen.ask.textContent = screen.busy ? 'Analysing…' : 'Ask about screen';
      }
    }

    async function enableCamera() {
      if (!local || disposed) return;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { status('Camera capture is unavailable. Open local Jarvis in a current browser on your Mac.'); return; }
      const generation = ++generations.camera;
      camera.requesting = true;
      refresh();
      status('Choose whether to allow the camera in your browser.');
      try {
        await setAccess('camera', true);
        if (!isCurrent('camera', generation)) return;
        const stream = await navigator.mediaDevices.getUserMedia({video: true, audio: false});
        if (!isCurrent('camera', generation)) { stopTracks(stream); return; }
        camera.stream = stream;
        cameraVideo.srcObject = stream;
        cameraVideo.play().catch(() => {});
        cameraBox.hidden = false;
        for (const track of stream.getTracks()) track.addEventListener('ended', () => { if (camera.stream === stream) stopCamera(); }, {once: true});
        status('Camera preview on. Jarvis receives a picture only when you press Ask about camera.');
      } catch (error) {
        if (isCurrent('camera', generation)) { stopCamera(false); report(error); }
      } finally {
        if (isCurrent('camera', generation)) camera.requesting = false;
        refresh();
      }
    }
    function stopCamera(announce = true) {
      const hadAccess = camera.stream || camera.requesting;
      camera.requesting = camera.busy = false;
      stopTracks(camera.stream);
      camera.stream = null;
      cameraVideo.srcObject = null;
      cameraBox.hidden = true;
      if (hadAccess) revoke('camera');
      refresh();
      if (announce) status('Camera off.');
    }
    async function enableMicrophone() {
      if (!local || disposed) return;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !(window.AudioContext || window.webkitAudioContext)) {
        status('Microphone recording is unavailable in this browser. Use a current browser on your Mac.'); return;
      }
      const generation = ++generations.microphone;
      microphone.requesting = true;
      refresh();
      status('Choose whether to allow the microphone in your browser.');
      try {
        await setAccess('microphone', true);
        if (!isCurrent('microphone', generation)) return;
        const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}, video: false});
        if (!isCurrent('microphone', generation)) { stopTracks(stream); return; }
        microphone.stream = stream;
        stream.getAudioTracks().forEach(track => { track.enabled = !speaking; });
        for (const track of stream.getTracks()) track.addEventListener('ended', () => { if (microphone.stream === stream) stopMicrophone(); }, {once: true});
        status('Microphone enabled. Press Record request when you want to speak.');
      } catch (error) {
        if (isCurrent('microphone', generation)) { stopMicrophone(false); report(error); }
      } finally {
        if (isCurrent('microphone', generation)) microphone.requesting = false;
        refresh();
      }
    }
    function cleanRecording(recording) {
      clearTimeout(recording.timer);
      recording.processor.onaudioprocess = null;
      for (const part of [recording.source, recording.processor, recording.gain]) {
        try { part.disconnect(); } catch (_) {}
      }
      recording.context.close().catch(() => {});
    }
    function stopMicrophone(announce = true) {
      const hadAccess = microphone.stream || microphone.requesting;
      microphone.requesting = microphone.busy = false;
      if (microphone.recording) {
        cleanRecording(microphone.recording);
        microphone.recording.chunks.length = 0;
        microphone.recording = null;
      }
      stopTracks(microphone.stream);
      microphone.stream = null;
      if (hadAccess) revoke('microphone');
      refresh();
      if (announce) status('Microphone off. Unsent recording discarded.');
    }
    async function startRecording() {
      if (!local || disposed || !microphone.stream || microphone.busy || microphone.recording || speaking) return;
      let context = null;
      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        // WebAudio supplies PCM at this context's rate, including resampling
        // higher-rate hardware, so the WAV matches local transcription bounds.
        context = new AudioContextClass({sampleRate: 48000});
        const recording = {
          context,
          source: context.createMediaStreamSource(microphone.stream),
          processor: context.createScriptProcessor(4096, 1, 1),
          gain: context.createGain(),
          chunks: [], count: 0, generation: generations.microphone, timer: null,
          // Keep even unusual high-rate hardware below the server's 3 MB JSON limit.
          maxSamples: Math.min(Math.floor(context.sampleRate * 10), 1000000)
        };
        microphone.recording = recording;
        recording.gain.gain.value = 0;
        recording.processor.onaudioprocess = event => {
          event.outputBuffer.getChannelData(0).fill(0);
          if (microphone.recording !== recording || speaking) return;
          const samples = event.inputBuffer.getChannelData(0);
          const remaining = recording.maxSamples - recording.count;
          if (remaining > 0) {
            const chunk = new Float32Array(samples.subarray(0, Math.min(samples.length, remaining)));
            recording.chunks.push(chunk); recording.count += chunk.length;
          }
          if (recording.count >= recording.maxSamples) finishRecording(true);
        };
        recording.source.connect(recording.processor);
        recording.processor.connect(recording.gain);
        recording.gain.connect(context.destination);
        recording.timer = setTimeout(() => { if (microphone.recording === recording) finishRecording(true); }, Math.min(10000, recording.maxSamples / context.sampleRate * 1000));
        refresh();
        status('Recording your request. Press Stop recording & send, or wait 10 seconds.');
        await context.resume();
      } catch (error) {
        if (microphone.recording && microphone.recording.context === context) {
          cleanRecording(microphone.recording); microphone.recording = null;
        } else if (context) context.close().catch(() => {});
        report(error); refresh();
      }
    }
    function wavBase64(recording) {
      const dataLength = recording.count * 2;
      const buffer = new ArrayBuffer(44 + dataLength), view = new DataView(buffer);
      const write = (offset, text) => { for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); };
      write(0, 'RIFF'); view.setUint32(4, 36 + dataLength, true); write(8, 'WAVE'); write(12, 'fmt ');
      view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
      view.setUint32(24, recording.context.sampleRate, true); view.setUint32(28, recording.context.sampleRate * 2, true);
      view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, dataLength, true);
      let offset = 44;
      for (const chunk of recording.chunks) for (const value of chunk) {
        const clamped = Math.max(-1, Math.min(1, value));
        view.setInt16(offset, clamped < 0 ? clamped * 32768 : clamped * 32767, true); offset += 2;
      }
      const bytes = new Uint8Array(buffer);
      let binary = '';
      for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      return btoa(binary);
    }
    async function finishRecording(send) {
      const recording = microphone.recording;
      if (!recording) return;
      microphone.recording = null;
      cleanRecording(recording);
      if (!send || speaking || !isCurrent('microphone', recording.generation) || !microphone.stream) {
        recording.chunks.length = 0; refresh(); return;
      }
      if (recording.count < recording.context.sampleRate * 0.15) {
        recording.chunks.length = 0; status('Recording was too short. Press Record request and speak for a moment.'); refresh(); return;
      }
      microphone.busy = true;
      refresh();
      status('Transcribing your request with local Jarvis…');
      try {
        const audio = wavBase64(recording);
        recording.chunks.length = 0;
        const result = await mediaJob('microphone', '/api/transcribe', {audio}, () => Boolean(microphone.stream));
        if (!result || !isCurrent('microphone', recording.generation)) return;
        const text = String(result.text || '').trim();
        if (!text) { status('No speech was recognized. Try recording again.'); return; }
        status('Recognized: ' + text);
        if (onTranscript) onTranscript(text);
      } catch (error) { if (isCurrent('microphone', recording.generation)) report(error); }
      finally {
        recording.chunks.length = 0;
        if (isCurrent('microphone', recording.generation)) microphone.busy = false;
        refresh();
      }
    }

    async function addScreen() {
      if (!local || disposed || screenRequests) return;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
        status('Screen sharing is unavailable in this browser. Try Chrome or Edge on your Mac; browser and macOS screen permissions may be needed.'); return;
      }
      const generation = generations.screen;
      screenRequests++;
      refresh();
      status('Choose the screen, window, or tab to share. Share again to add another screen.');
      try {
        // Browsers require the screen picker in this click's activation. The
        // selected stream remains unattached until server authorization succeeds.
        const access = setAccess('screen', true);
        let permission;
        try { permission = navigator.mediaDevices.getDisplayMedia({video: true, audio: false}); }
        catch (error) { await access.catch(() => {}); throw error; }
        const [authorization, choice] = await Promise.allSettled([access, permission]);
        if (authorization.status === 'rejected') {
          if (choice.status === 'fulfilled') stopTracks(choice.value);
          throw authorization.reason;
        }
        if (choice.status === 'rejected') throw choice.reason;
        const stream = choice.value;
        if (!isCurrent('screen', generation)) { stopTracks(stream); return; }
        const id = ++screenNumber;
        const screen = {id, stream, busy: false};
        const box = node('div', null, screensBox);
        box.style.cssText = 'margin-top:12px;padding-top:10px;border-top:1px solid #163248';
        const heading = row(box);
        node('span', 'Shared screen ' + id, heading);
        button('Stop this screen', heading, () => stopScreen(screen));
        screen.video = preview(box, 'Local preview of shared screen ' + id);
        screen.video.srcObject = stream;
        screen.video.play().catch(() => {});
        const question = row(box);
        screen.prompt = node('input', null, question);
        screen.prompt.type = 'text'; screen.prompt.maxLength = 2000;
        screen.prompt.placeholder = 'Ask about this screen…';
        screen.prompt.setAttribute('aria-label', 'Question about shared screen ' + id);
        screen.prompt.style.flex = '1 1 170px';
        screen.ask = button('Ask about screen', question, () => askAbout('screen', screen, screen.video, screen.prompt, screen.ask));
        screen.box = box;
        screens.set(id, screen);
        for (const track of stream.getTracks()) track.addEventListener('ended', () => stopScreen(screen), {once: true});
        status('Screen preview on. Jarvis receives one picture only when you press Ask about screen.');
      } catch (error) {
        if (isCurrent('screen', generation)) {
          if (!screens.size) revoke('screen');
          report(error);
        }
      } finally {
        screenRequests = Math.max(0, screenRequests - 1);
        refresh();
      }
    }
    function stopScreen(screen, announce = true, revokeLast = true) {
      if (!screens.has(screen.id)) return;
      screens.delete(screen.id);
      stopTracks(screen.stream);
      screen.video.srcObject = null;
      screen.box.remove();
      if (!screens.size && revokeLast) revoke('screen');
      refresh();
      if (announce) status(screens.size ? 'That screen stopped. Other shared screens remain on.' : 'Screen sharing off.');
    }
    async function askAbout(source, device, video, input, askButton) {
      if (!local || disposed || device.busy || !device.stream) return;
      const valid = () => source === 'camera' ? camera.stream === device.stream && Boolean(camera.stream) : screens.get(device.id) === device;
      if (!valid()) return;
      if (!video.videoWidth || !video.videoHeight || video.readyState < 2) { status('The video preview is not ready yet. Wait a moment and try again.'); return; }
      const generation = generations[source];
      const prompt = input.value.trim() || 'Describe what you see and offer any useful observations.';
      device.busy = true;
      refresh();
      status('Sending one ' + source + ' snapshot for your question…');
      try {
        const canvas = document.createElement('canvas');
        const scale = Math.min(1, 1024 / Math.max(video.videoWidth, video.videoHeight));
        canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
        const context = canvas.getContext('2d');
        if (!context) throw new Error('This browser could not prepare an image snapshot.');
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const image = canvas.toDataURL('image/jpeg', 0.8).split(',')[1];
        // Discard the drawing as soon as the single requested frame is encoded.
        canvas.width = canvas.height = 0;
        if (addMessage) addMessage('YOU', '[' + (source === 'camera' ? 'Camera' : 'Screen ' + device.id) + ' snapshot] ' + prompt, 'user');
        const result = await mediaJob(source, '/api/perception', {source, image, prompt}, valid);
        if (!result || !isCurrent(source, generation) || !valid()) return;
        const reply = String(result.reply || 'No observation was returned.');
        if (onAnalysis) onAnalysis(reply);
        else if (addMessage) addMessage('JARVIS', reply);
        status('Snapshot analysis complete. No further pictures are being sent.');
      } catch (error) { if (isCurrent(source, generation) && valid()) report(error); }
      finally { if (isCurrent(source, generation) && valid()) device.busy = false; refresh(); }
    }
    function stopAll() {
      if (!local) return;
      stopCamera(false);
      stopMicrophone(false);
      const hadScreens = screens.size || screenRequests;
      for (const screen of Array.from(screens.values())) stopScreen(screen, false, false);
      if (hadScreens && !screens.size) revoke('screen');
      refresh();
    }
    function setSpeaking(value) {
      speaking = Boolean(value);
      if (speaking && microphone.recording) {
        finishRecording(false);
        status('Recording discarded and microphone paused while Jarvis speaks.');
      }
      if (microphone.stream) microphone.stream.getAudioTracks().forEach(track => { track.enabled = !speaking; });
      refresh();
    }
    function dispose() {
      stopAll();
      disposed = true;
      for (const source of sources) generations[source]++;
      for (const job of jobs) job.controller.abort();
      window.removeEventListener('pagehide', handlePageHide);
      refresh();
    }
    function handlePageHide() { stopAll(); }
    window.addEventListener('pagehide', handlePageHide);
    refresh();
    return {
      setSpeaking, stopAll, dispose,
      getState: () => ({camera: Boolean(camera.stream), microphone: Boolean(microphone.stream), screens: screens.size, recording: Boolean(microphone.recording), speaking})
    };
  }
  window.JarvisDevices = Object.freeze({mount});
}());
