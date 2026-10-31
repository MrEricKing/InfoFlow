/*
 * InfoFlow automation bridge for programmatic SingleFile captures.
 *
 * This layer keeps a requestId-scoped status model around the existing
 * SingleFile save pipeline so backend automation can observe terminal
 * outcomes without depending only on browser-level download events.
 */

const CAPTURE_STATUS_QUEUED = "queued";
const CAPTURE_STATUS_RUNNING = "running";
const CAPTURE_STATUS_DOWNLOAD_STARTED = "download_started";
const CAPTURE_STATUS_SUCCEEDED = "succeeded";
const CAPTURE_STATUS_FAILED = "failed";
const CAPTURE_STATUS_CANCELLED = "cancelled";
const TERMINAL_STATUSES = new Set([CAPTURE_STATUS_SUCCEEDED, CAPTURE_STATUS_FAILED, CAPTURE_STATUS_CANCELLED]);
const records = new Map();
const taskRequests = new Map();

export {
	createRequest,
	registerTasks,
	getStatus,
	listStatuses,
	buildSaveOptions,
	onDownloadRequested,
	onDownloadStarted,
	onDownloadComplete,
	onDownloadInterrupted,
	onDownloadError,
	onTaskEnded,
	onTaskError,
	onTaskCancelled
};

function createRequest(message, tab, sender) {
	const requestId = normalizeRequestId(message.requestId);
	if (!requestId) {
		throw new Error("infoflow.capture.start requires requestId");
	}
	const now = new Date().toISOString();
	const existing = records.get(requestId);
	if (existing && !TERMINAL_STATUSES.has(existing.status)) {
		return cloneStatus(existing);
	}
	const record = {
		requestId,
		status: CAPTURE_STATUS_QUEUED,
		createdAt: now,
		updatedAt: now,
		senderUrl: sender && sender.url || null,
		tab: tab ? mapTab(tab) : null,
		tasks: [],
		downloads: [],
		errors: [],
		completedAt: null,
		saveOptions: null
	};
	records.set(requestId, record);
	return cloneStatus(record);
}

function registerTasks(requestId, tasks, saveOptions) {
	const record = requireRecord(requestId);
	record.status = tasks && tasks.length ? CAPTURE_STATUS_RUNNING : CAPTURE_STATUS_FAILED;
	record.tasks = (tasks || []).map(task => ({
		id: task.id,
		tabId: task.tabId,
		url: task.url,
		title: task.title,
		status: task.status,
		cancelled: Boolean(task.cancelled)
	}));
	record.saveOptions = sanitizeSaveOptions(saveOptions);
	if (!tasks || !tasks.length) {
		record.errors.push({ stage: "queue", message: "SingleFile did not create a task" });
		record.completedAt = new Date().toISOString();
	}
	for (const task of tasks || []) {
		if (typeof task.id == "number") {
			taskRequests.set(task.id, requestId);
		}
	}
	touch(record);
	return cloneStatus(record);
}

function getStatus(requestId) {
	const record = records.get(normalizeRequestId(requestId));
	return record ? cloneStatus(record) : null;
}

function listStatuses() {
	return Array.from(records.values()).map(cloneStatus);
}

function buildSaveOptions(message) {
	const options = {
		backgroundSave: true,
		confirmFilename: false,
		openSavedPage: false,
		filenameConflictAction: "uniquify",
		selfExtractingArchive: false,
		compressContent: false,
		blockScripts: true,
		removeHiddenElements: false,
		removeUnusedStyles: false,
		loadDeferredImages: true,
		blockAlternativeImages: false,
		removeAlternativeImages: false,
		removeAlternativeMedias: false,
		infoflowRequestId: normalizeRequestId(message.requestId)
	};
	if (message.options && typeof message.options == "object") {
		Object.assign(options, sanitizeIncomingOptions(message.options));
	}
	return options;
}

function onDownloadRequested(requestId, details) {
	updateRecord(requestId, record => {
		record.status = CAPTURE_STATUS_RUNNING;
		record.downloads.push({
			status: "requested",
			requestedAt: new Date().toISOString(),
			downloadId: null,
			filename: details && details.downloadInfo && details.downloadInfo.filename || null,
			resolvedFilename: null,
			error: null
		});
	});
}

function onDownloadStarted(requestId, details) {
	updateRecord(requestId, record => {
		record.status = CAPTURE_STATUS_DOWNLOAD_STARTED;
		const download = latestDownload(record);
		download.status = "started";
		download.startedAt = new Date().toISOString();
		download.downloadId = details && details.downloadId || null;
		download.filename = details && details.downloadInfo && details.downloadInfo.filename || download.filename || null;
	});
}

function onDownloadComplete(requestId, details) {
	updateRecord(requestId, record => {
		const download = latestDownload(record);
		download.status = "complete";
		download.completedAt = new Date().toISOString();
		download.downloadId = details && details.downloadId || download.downloadId || null;
		download.resolvedFilename = details && details.filename || null;
		record.status = CAPTURE_STATUS_SUCCEEDED;
		record.completedAt = download.completedAt;
	});
}

function onDownloadInterrupted(requestId, details) {
	updateRecord(requestId, record => {
		const download = latestDownload(record);
		download.status = details && details.cancelled ? "cancelled" : "interrupted";
		download.completedAt = new Date().toISOString();
		download.downloadId = details && details.downloadId || download.downloadId || null;
		download.error = details && details.error || null;
		record.status = details && details.cancelled ? CAPTURE_STATUS_CANCELLED : CAPTURE_STATUS_FAILED;
		record.completedAt = download.completedAt;
		if (download.error) {
			record.errors.push({ stage: "download", message: download.error });
		}
	});
}

function onDownloadError(requestId, error) {
	updateRecord(requestId, record => {
		const download = latestDownload(record);
		download.status = "failed";
		download.completedAt = new Date().toISOString();
		download.error = error && (error.message || error.toString()) || "download failed";
		record.status = CAPTURE_STATUS_FAILED;
		record.completedAt = download.completedAt;
		record.errors.push({ stage: "download", message: download.error });
	});
}

function onTaskEnded(taskId, details = {}) {
	const requestId = taskRequests.get(taskId);
	if (!requestId) {
		return;
	}
	updateRecord(requestId, record => {
		for (const task of record.tasks) {
			if (task.id == taskId) {
				task.status = "done";
			}
		}
		record.hash = details.hash || record.hash || null;
		if (!TERMINAL_STATUSES.has(record.status)) {
			record.status = CAPTURE_STATUS_SUCCEEDED;
			record.completedAt = new Date().toISOString();
		}
	});
	taskRequests.delete(taskId);
}

function onTaskError(taskId, error) {
	const requestId = taskRequests.get(taskId);
	if (!requestId) {
		return;
	}
	updateRecord(requestId, record => {
		const message = error && (error.message || error.toString()) || "capture task failed";
		record.status = CAPTURE_STATUS_FAILED;
		record.completedAt = new Date().toISOString();
		record.errors.push({ stage: "task", message });
		for (const task of record.tasks) {
			if (task.id == taskId) {
				task.status = "failed";
			}
		}
	});
	taskRequests.delete(taskId);
}

function onTaskCancelled(taskId) {
	const requestId = taskRequests.get(taskId);
	if (!requestId) {
		return;
	}
	updateRecord(requestId, record => {
		record.status = CAPTURE_STATUS_CANCELLED;
		record.completedAt = new Date().toISOString();
		for (const task of record.tasks) {
			if (task.id == taskId) {
				task.status = "cancelled";
				task.cancelled = true;
			}
		}
	});
	taskRequests.delete(taskId);
}

function updateRecord(requestId, updater) {
	const normalizedRequestId = normalizeRequestId(requestId);
	if (!normalizedRequestId) {
		return;
	}
	const record = records.get(normalizedRequestId);
	if (!record) {
		return;
	}
	updater(record);
	touch(record);
}

function requireRecord(requestId) {
	const record = records.get(normalizeRequestId(requestId));
	if (!record) {
		throw new Error("Unknown InfoFlow capture request");
	}
	return record;
}

function latestDownload(record) {
	if (!record.downloads.length) {
		record.downloads.push({
			status: "unknown",
			requestedAt: new Date().toISOString(),
			downloadId: null,
			filename: null,
			resolvedFilename: null,
			error: null
		});
	}
	return record.downloads[record.downloads.length - 1];
}

function touch(record) {
	record.updatedAt = new Date().toISOString();
}

function cloneStatus(record) {
	return JSON.parse(JSON.stringify(record));
}

function normalizeRequestId(value) {
	return typeof value == "string" && value.trim() ? value.trim().slice(0, 128) : null;
}

function mapTab(tab) {
	return {
		id: tab.id,
		index: tab.index,
		windowId: tab.windowId,
		url: tab.url,
		title: tab.title
	};
}

function sanitizeIncomingOptions(options) {
	const allowedKeys = new Set([
		"backgroundSave",
		"confirmFilename",
		"openSavedPage",
		"filenameConflictAction",
		"selfExtractingArchive",
		"compressContent",
		"blockScripts",
		"removeHiddenElements",
		"removeUnusedStyles",
		"loadDeferredImages",
		"blockAlternativeImages",
		"removeAlternativeImages",
		"removeAlternativeMedias",
		"insertMetaCSP",
		"applySystemTheme",
		"includeInfobar",
		"openInfobar"
	]);
	return Object.fromEntries(Object.entries(options).filter(([key]) => allowedKeys.has(key)));
}

function sanitizeSaveOptions(options) {
	const clone = { ...options };
	delete clone.infoflowRequestId;
	return clone;
}
