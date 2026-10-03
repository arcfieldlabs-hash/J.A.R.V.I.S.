/* Local source inspection, reviewed code changes, and explicit execution approval. */
(function () {
  'use strict';

  function mount(options) {
    const {container, enabled, api, addMessage} = options;
    if (!container) throw new Error('A development panel container is required.');
    const local = Boolean(enabled);
    const jobs = new Set();
    const approvals = new Map();
    const resolved = new Set();
    const actionControls = new Set();
    let disposed = false, permissionPending = false, allowed = false;
    let pollTimer = null, lastProposalKey = '', lastExtensionKey = '';
    let latestProposals = [];

    function node(tag, text, parent) {
      const element = document.createElement(tag);
      if (text != null) element.textContent = String(text);
      if (parent) parent.append(element);
      return element;
    }
    function row(parent) {
      const element = node('div', null, parent);
      element.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0';
      return element;
    }
    function button(text, parent, handler) {
      const element = node('button', text, parent);
      element.type = 'button';
      element.style.cssText = 'border:1px solid #2f81a4;border-radius:7px;background:#164961;color:#d8f5ff;padding:7px 10px';
      element.addEventListener('click', handler);
      return element;
    }
    function card(parent) {
      const element = node('div', null, parent);
      element.style.cssText = 'margin-top:10px;padding:12px;border:1px solid #204359;border-radius:7px;overflow-wrap:anywhere';
      return element;
    }
    function pre(text, parent, label) {
      const element = node('pre', text, parent);
      if (label) element.setAttribute('aria-label', label);
      element.tabIndex = 0;
      element.style.cssText = 'margin:8px 0;max-height:300px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#06101a;padding:10px;border-radius:5px;font:11px/1.5 ui-monospace,SFMono-Regular,monospace';
      return element;
    }
    function detail(text, parent) {
      const element = node('p', text, parent);
      element.style.cssText = 'font-size:11px;color:#8194a6;margin:6px 0';
      return element;
    }
    function errorText(error) { return error && error.message ? error.message : 'The request failed.'; }
    function report(error) { status.textContent = errorText(error); }
    function actionAllowed(action) { return local && !permissionPending && (allowed || action === 'rollback'); }
    function post(path, payload) {
      return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    }
    function artifactData(value) {
      if (!value || typeof value !== 'object') return {};
      const nested = value.proposal && typeof value.proposal === 'object' ? value.proposal : {};
      return {...nested, ...value};
    }
    function renderArtifact(value, parent) {
      const data = artifactData(value);
      parent.replaceChildren();
      if (data.digest) {
        const digest = detail('SHA-256: ' + data.digest, parent);
        digest.style.overflowWrap = 'anywhere';
      }
      let hasContent = false;
      for (const key of ['diff', 'code']) {
        if (typeof data[key] === 'string') {
          node('strong', key === 'diff' ? 'Complete change' : 'Complete tool code', parent);
          pre(data[key] || '(Empty)', parent, key === 'diff' ? 'Complete proposed diff' : 'Complete proposed tool code');
          hasContent = true;
        }
      }
      const metadata = {};
      for (const [key, value] of Object.entries(data)) {
        if (!['diff', 'code', 'proposal', 'digest'].includes(key)) metadata[key] = value;
      }
      if (Object.keys(metadata).length) pre(JSON.stringify(metadata, null, 2), parent, 'Review details');
      if (!hasContent && !Object.keys(metadata).length) detail('No change details were provided.', parent);
      return hasContent;
    }

    container.style.cssText = 'margin-top:16px;padding:16px;border:1px solid #163248;border-radius:9px';
    const heading = node('h2', 'Build and extend Jarvis', container);
    heading.style.cssText = 'margin:0 0 12px;font-size:14px;font-weight:500';
    const permissionRow = row(container);
    const permissionLabel = node('label', null, permissionRow);
    const permission = node('input', null, permissionLabel);
    permission.type = 'checkbox';
    permission.disabled = !local;
    permissionLabel.append(document.createTextNode(' Allow code development'));
    detail('Inspect source, propose changes, and add tools. You approve each test, installation, or rollback before it runs. Rollback remains available when development is off.', container);
    detail('Tests and installed tools execute Python with your Mac user’s permissions.', container);
    const status = detail(local ? 'Code development is off until you enable it.' : 'Open Jarvis on your Mac to develop and approve tools. This hosted page is a preview.', container);
    status.setAttribute('role', 'status');

    const sourceDetails = node('details', null, container);
    node('summary', 'Inspect Jarvis source', sourceDetails);
    const sourceForm = node('form', null, sourceDetails);
    sourceForm.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:10px 0';
    const pathInput = node('input', null, sourceForm);
    pathInput.type = 'text';
    pathInput.value = '.';
    pathInput.setAttribute('aria-label', 'Source file or directory');
    pathInput.style.cssText = 'flex:1;min-width:130px';
    const lineInput = node('input', null, sourceForm);
    lineInput.type = 'number';
    lineInput.value = '1';
    lineInput.min = '1';
    lineInput.step = '1';
    lineInput.setAttribute('aria-label', 'Starting line');
    lineInput.style.cssText = 'width:65px;color:#d5e9f8;background:#06101a;border:1px solid #204359;border-radius:5px;padding:6px';
    const inspect = button('Inspect', sourceForm, () => sourceForm.requestSubmit());
    inspect.disabled = !local;
    pathInput.disabled = !local;
    lineInput.disabled = !local;
    const sourceContent = node('div', null, sourceDetails);

    const approvalHeading = node('h3', 'Awaiting your approval', container);
    approvalHeading.style.cssText = 'font-size:12px;margin:18px 0 8px';
    const approvalList = node('div', null, container);
    const approvalEmpty = detail('No actions awaiting approval.', approvalList);
    const proposalHeading = node('h3', 'Proposed changes', container);
    proposalHeading.style.cssText = 'font-size:12px;margin:18px 0 8px';
    const proposalList = node('div', null, container);
    detail(local ? 'Ask Jarvis to propose a tool or a source change.' : 'Proposals are available in the local app.', proposalList);
    const extensionHeading = node('h3', 'Installed tools', container);
    extensionHeading.style.cssText = 'font-size:12px;margin:18px 0 8px';
    const extensionList = node('div', null, container);
    detail('No added tools.', extensionList);

    permission.addEventListener('change', async () => {
      if (!local || permissionPending || disposed) return;
      const requested = permission.checked;
      permissionPending = true;
      permission.disabled = true;
      for (const control of actionControls) control.disabled = true;
      status.textContent = requested ? 'Enabling code development…' : 'Turning code development off…';
      try {
        const result = await post('/api/settings', {self_development: requested});
        if (disposed) return;
        allowed = typeof result.self_development === 'boolean' ? result.self_development : (result.development && typeof result.development.enabled === 'boolean' ? result.development.enabled : requested);
        permission.checked = allowed;
        status.textContent = allowed ? 'Code development enabled. Execution still requires your approval.' : 'Code development off. Pending development approvals are cancelled.';
        lastProposalKey = '';
        await pollApprovals();
      } catch (error) {
        if (!disposed) { permission.checked = allowed; report(error); }
      } finally {
        permissionPending = false;
        if (!disposed) {
          permission.disabled = !local;
          for (const control of actionControls) control.disabled = !actionAllowed(control.dataset.action) || jobs.size > 0;
          renderProposals(latestProposals);
        }
      }
    });

    sourceForm.addEventListener('submit', async event => {
      event.preventDefault();
      if (!local || disposed) return;
      inspect.disabled = true;
      try {
        const query = new URLSearchParams({path: pathInput.value.trim() || '.', line: String(Math.max(1, Number(lineInput.value) || 1))});
        const result = await api('/api/development/source?' + query);
        if (disposed) return;
        sourceContent.replaceChildren();
        if (result.path) detail(result.path, sourceContent);
        if (typeof result.content === 'string') {
          pre(result.content, sourceContent, 'Source contents');
          if (result.truncated || result.has_more || result.next_line) detail('More source is available. Increase the starting line to continue reading.', sourceContent);
        } else pre(JSON.stringify(result.files || result, null, 2), sourceContent, 'Source files');
      } catch (error) { if (!disposed) report(error); }
      finally { if (!disposed) inspect.disabled = !local; }
    });

    async function reviewProposal(id, parent) {
      const result = await api('/api/development/proposals/' + encodeURIComponent(id));
      if (!disposed) renderArtifact(result, parent);
      return result;
    }

    async function runAction(action, id, output, controls) {
      if (disposed || !actionAllowed(action)) return;
      controls.forEach(control => { control.disabled = true; });
      const job = {id: null, cancelled: false};
      jobs.add(job);
      output.textContent = 'Requesting ' + action + '…';
      try {
        const queued = await post('/api/development/action', {action, id});
        if (!queued.request_id) throw new Error('The local server did not return a request ID.');
        job.id = queued.request_id;
        output.textContent = 'Awaiting your approval or execution result. Review the approval card above.';
        while (!disposed && !job.cancelled) {
          await new Promise(resolve => setTimeout(resolve, 500));
          if (disposed || job.cancelled) return;
          const result = await api('/api/development/jobs/' + encodeURIComponent(job.id));
          if (disposed || job.cancelled) return;
          if (result.status === 'pending' || result.status === 'running' || result.status === 'awaiting_approval') continue;
          if (result.status === 'error') throw new Error(result.error || 'The development action failed.');
          if (result.status !== 'complete') throw new Error('The server returned an unexpected development status.');
          output.textContent = typeof result.result === 'string' ? result.result : JSON.stringify(result.result || result, null, 2);
          if (addMessage) addMessage('SYSTEM', 'Development ' + action + ' completed. Review the result in the code development panel.', 'system');
          lastProposalKey = '';
          break;
        }
      } catch (error) { if (!disposed) { output.textContent = errorText(error); report(error); } }
      finally {
        jobs.delete(job);
        if (!disposed) controls.forEach(control => { control.disabled = !actionAllowed(control.dataset.action); });
      }
    }

    function renderProposals(list) {
      const key = JSON.stringify([allowed, list]);
      if (key === lastProposalKey) return;
      // Keep active job results and their controls stable while an approval is pending.
      if (jobs.size) return;
      lastProposalKey = key;
      proposalList.replaceChildren();
      actionControls.clear();
      if (!list.length) { detail('Ask Jarvis to propose a tool or a source change.', proposalList); return; }
      for (const proposal of list) {
        const element = card(proposalList);
        node('strong', proposal.title || proposal.name || 'Proposed change', element);
        detail((proposal.kind || 'Code') + ' · ' + (proposal.status || 'Proposed') + ' · ' + proposal.id, element);
        if (proposal.description) detail(proposal.description, element);
        const controlsRow = row(element);
        const reviewContent = node('div', null, element);
        const review = button('Review complete change', controlsRow, async () => {
          review.disabled = true;
          try { await reviewProposal(proposal.id, reviewContent); }
          catch (error) { if (!disposed) report(error); }
          finally { if (!disposed) review.disabled = !local; }
        });
        review.disabled = !local;
        const controls = [];
        const output = pre('', element, 'Development action result');
        for (const [action, label] of [['test', 'Run tests'], ['apply', 'Apply'], ['rollback', 'Roll back']]) {
          const control = button(label, controlsRow, () => runAction(action, proposal.id, output, controls));
          control.dataset.action = action;
          control.disabled = !actionAllowed(action);
          controls.push(control);
          actionControls.add(control);
        }
        detail('Each execution asks for a separate approval. Applying a source change may require restarting Jarvis.', element);
      }
    }

    function renderExtensions(list) {
      const key = JSON.stringify(list);
      if (key === lastExtensionKey) return;
      lastExtensionKey = key;
      extensionList.replaceChildren();
      if (!list.length) { detail('No added tools.', extensionList); return; }
      for (const extension of list) {
        const element = card(extensionList);
        node('strong', extension.name || extension.id || 'Added tool', element);
        if (extension.description) detail(extension.description, element);
        if (extension.digest) detail('SHA-256: ' + extension.digest, element);
      }
    }

    function renderApprovals(list) {
      const current = new Set();
      for (const approval of list) {
        const id = String(approval.id);
        if (resolved.has(id) || (approval.status && approval.status !== 'pending')) continue;
        current.add(id);
        if (approvals.has(id)) continue;
        const element = card(approvalList);
        const approvalLabels = {'selfdev.test': 'Run tests for this change?', 'selfdev.apply': 'Install this change?', 'selfdev.rollback': 'Roll back this change?'};
        node('strong', approvalLabels[approval.summary] || approval.summary || 'Approve this action?', element);
        const reviewContent = node('div', null, element);
        const details = artifactData(approval.details);
        const hasArtifact = renderArtifact(details, reviewContent);
        const resultStatus = detail('', element);
        resultStatus.setAttribute('role', 'status');
        const controlsRow = row(element);
        const approve = button('Approve once', controlsRow, () => answer(true));
        const reject = button('Reject', controlsRow, () => answer(false));
        const proposalId = details.proposal_id || details.proposalId;
        // Never offer approval for a hidden code artifact. Fetch its entire review first.
        approve.disabled = !local || Boolean(proposalId && !hasArtifact);
        reject.disabled = !local;
        const entry = {element, approve, reject, busy: false};
        approvals.set(id, entry);
        if (proposalId && !hasArtifact && local) {
          resultStatus.textContent = 'Loading the complete change before approval…';
          reviewProposal(proposalId, reviewContent).then(() => {
            if (!disposed && approvals.has(id)) { approve.disabled = false; resultStatus.textContent = ''; }
          }).catch(error => { if (!disposed && approvals.has(id)) resultStatus.textContent = 'Cannot load the complete change: ' + errorText(error); });
        }
        async function answer(approved) {
          if (!local || disposed || entry.busy) return;
          entry.busy = true;
          approve.disabled = reject.disabled = true;
          resultStatus.textContent = approved ? 'Approving this action…' : 'Rejecting this action…';
          try {
            await post('/api/approvals/' + encodeURIComponent(id), {approved});
            if (disposed) return;
            resolved.add(id);
            if (resolved.size > 500) resolved.delete(resolved.values().next().value);
            approvals.delete(id);
            element.remove();
            approvalEmpty.hidden = approvals.size > 0;
            status.textContent = approved ? 'Action approved once.' : 'Action rejected.';
          } catch (error) {
            if (!disposed) { resultStatus.textContent = errorText(error); approve.disabled = reject.disabled = false; }
          } finally { entry.busy = false; }
        }
      }
      for (const [id, entry] of approvals) {
        if (!current.has(id) && !entry.busy) { entry.element.remove(); approvals.delete(id); }
      }
      approvalEmpty.hidden = approvals.size > 0;
    }

    async function pollApprovals() {
      if (!local || disposed) return;
      try {
        const result = await api('/api/approvals');
        if (!disposed) renderApprovals(Array.isArray(result.approvals) ? result.approvals : []);
      } catch (error) {
        if (!disposed) status.textContent = 'Could not refresh approval requests: ' + errorText(error);
      }
    }
    async function poll() {
      if (disposed) return;
      await pollApprovals();
      if (!disposed) pollTimer = setTimeout(poll, 1000);
    }
    function refresh(state) {
      if (disposed || !local) return;
      const development = state && state.development || {};
      if (!permissionPending) {
        const changed = allowed !== Boolean(development.enabled);
        allowed = Boolean(development.enabled);
        permission.checked = allowed;
        if (changed) status.textContent = allowed ? 'Code development enabled. Execution still requires your approval.' : 'Code development off. Pending development approvals are cancelled.';
        if (!allowed) for (const control of actionControls) if (control.dataset.action !== 'rollback') control.disabled = true;
      }
      latestProposals = Array.isArray(development.proposals) ? development.proposals : [];
      renderProposals(latestProposals);
      renderExtensions(Array.isArray(development.extensions) ? development.extensions : []);
      if (state && Array.isArray(state.approvals)) renderApprovals(state.approvals);
    }
    function dispose() {
      disposed = true;
      if (pollTimer != null) clearTimeout(pollTimer);
      for (const job of jobs) job.cancelled = true;
      jobs.clear();
    }
    if (local) poll();
    window.addEventListener('pagehide', dispose, {once: true});
    return Object.freeze({refresh, dispose});
  }

  window.JarvisDevelopment = Object.freeze({mount});
}());
