/*
 * Copyright 2010-2020 Gildas Lormeau
 * contact : gildas.lormeau <at> gmail.com
 *
 * This file is part of SingleFile.
 *
 *   The code in this file is free software: you can redistribute it and/or
 *   modify it under the terms of the GNU Affero General Public License
 *   (GNU AGPL) as published by the Free Software Foundation, either version 3
 *   of the License, or (at your option) any later version.
 *
 *   The code in this file is distributed in the hope that it will be useful,
 *   but WITHOUT ANY WARRANTY; without even the implied warranty of
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
 *   General Public License for more details.
 *
 *   As additional permission under GNU AGPL version 3 section 7, you may
 *   distribute UNMODIFIED VERSIONS OF THIS file without the copy of the GNU
 *   AGPL normally required by section 4, provided you include this license
 *   notice and a URL through which recipients can access the Corresponding
 *   Source.
 */

/* global browser */

import * as business from "./business.js";
import * as infoflowAutomation from "./infoflow-automation.js";
import "./../../lib/single-file/background.js";

const INFOFLOW_METHOD_CAPTURE_START = "infoflow.capture.start";
const INFOFLOW_METHOD_CAPTURE_STATUS = "infoflow.capture.status";
const INFOFLOW_METHOD_CAPTURE_LIST = "infoflow.capture.list";
const INFOFLOW_METHOD_CAPTURE_CANCEL = "infoflow.capture.cancel";

export { onMessage };

async function onMessage(message, sender) {
	if (message && typeof message == "object" && typeof message.method == "string" && message.method.startsWith("infoflow.capture.")) {
		return onInfoFlowMessage(message, sender);
	}
	return false;
}

async function onInfoFlowMessage(message, sender) {
	if (message.method == INFOFLOW_METHOD_CAPTURE_START) {
		const tabs = await browser.tabs.query({ currentWindow: true, active: true });
		tabs.length = 1;
		const tab = tabs[0];
		infoflowAutomation.createRequest(message, tab, sender);
		const saveOptions = infoflowAutomation.buildSaveOptions(message);
		const tasks = tab ? await business.saveTabs(tabs, saveOptions) : [];
		return infoflowAutomation.registerTasks(message.requestId, tasks, saveOptions);
	}
	if (message.method == INFOFLOW_METHOD_CAPTURE_STATUS) {
		return infoflowAutomation.getStatus(message.requestId);
	}
	if (message.method == INFOFLOW_METHOD_CAPTURE_LIST) {
		return infoflowAutomation.listStatuses();
	}
	if (message.method == INFOFLOW_METHOD_CAPTURE_CANCEL) {
		const status = infoflowAutomation.getStatus(message.requestId);
		if (status) {
			for (const task of status.tasks || []) {
				if (typeof task.id == "number") {
					business.cancelTask(task.id);
					infoflowAutomation.onTaskCancelled(task.id);
				}
			}
		}
		return infoflowAutomation.getStatus(message.requestId);
	}
	throw new Error("Unknown InfoFlow capture method");
}
