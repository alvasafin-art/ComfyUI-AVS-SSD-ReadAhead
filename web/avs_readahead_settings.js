import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SETTING_ID = "AVS.ReadAhead.Enabled";
let syncingFromBackend = false;

async function getBackendStatus() {
    const response = await api.fetchApi("/avs-readahead/status");
    if (!response.ok) {
        throw new Error(`status request failed: HTTP ${response.status}`);
    }
    return await response.json();
}

async function setBackendEnabled(enabled) {
    const response = await api.fetchApi("/avs-readahead/enabled", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: Boolean(enabled) }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(payload.error || `update failed: HTTP ${response.status}`);
    }
    return payload;
}

app.registerExtension({
    name: "AVS.ReadAhead.Settings",
    settings: [
        {
            id: SETTING_ID,
            name: "Enable AVS SSD ReadAhead",
            type: "boolean",
            defaultValue: true,
            category: ["AVS-ReadAhead", "ReadAhead", "Enabled"],
            tooltip: "Warm large .safetensors/.sft files in the Windows file cache. Changes apply immediately; no folder removal or restart is required.",
            onChange: async (newValue, oldValue) => {
                // ComfyUI invokes onChange once while registering the setting.
                // Backend config is the durable source of truth, so initial
                // registration must not overwrite an existing disabled state.
                if (oldValue === undefined || syncingFromBackend) {
                    return;
                }
                try {
                    await setBackendEnabled(Boolean(newValue));
                } catch (error) {
                    console.error("[AVS ReadAhead] Could not update setting:", error);
                    // Revert the UI when persistence/runtime update fails.
                    syncingFromBackend = true;
                    try {
                        await app.extensionManager.setting.set(SETTING_ID, Boolean(oldValue));
                    } finally {
                        syncingFromBackend = false;
                    }
                }
            },
        },
    ],
    async setup() {
        try {
            const status = await getBackendStatus();
            const uiValue = app.extensionManager.setting.get(SETTING_ID);
            if (Boolean(uiValue) !== Boolean(status.enabled)) {
                syncingFromBackend = true;
                try {
                    await app.extensionManager.setting.set(SETTING_ID, Boolean(status.enabled));
                } finally {
                    syncingFromBackend = false;
                }
            }
        } catch (error) {
            console.error("[AVS ReadAhead] Could not synchronize Application Settings:", error);
        }
    },
});
