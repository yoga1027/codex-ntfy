package local.codexntfy.opush;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.AtomicFile;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Phone-local directory and transcript cache, shared by the list and receiver. */
final class ThreadStore {
    static final String CATALOG = "catalog";
    static final String PROJECTS = "projects_catalog";
    static final String HIDE_ARCHIVED = "hide_archived";
    private static final Pattern ID = Pattern.compile("[A-Za-z0-9-]{1,100}");
    final SharedPreferences preferences;
    private final File directory;

    ThreadStore(Context context) {
        preferences = context.getSharedPreferences("codex_threads", Context.MODE_PRIVATE);
        directory = new File(context.getFilesDir(), "codex_threads");
    }

    static String threadFromEvent(String eventId) {
        if (eventId == null) {
            return null;
        }
        int separator = eventId.indexOf(':');
        if (separator <= 0 || separator == eventId.length() - 1) {
            return null;
        }
        String thread = eventId.substring(0, separator);
        return validId(thread) ? thread : null;
    }

    static boolean validId(String id) {
        return id != null && !"external".equals(id) && ID.matcher(id).matches();
    }

    static boolean isClaude(String id) {
        return id != null && id.startsWith("claude-");
    }

    void saveCatalog(JSONObject catalog) {
        JSONArray threads = catalog == null ? null : catalog.optJSONArray("threads");
        if (threads == null) {
            return;
        }
        Map<String, JSONObject> entries = new LinkedHashMap<>();
        addCatalog(threads, entries, false);
        try {
            addCatalog(new JSONArray(preferences.getString(CATALOG, "[]")), entries, true);
        } catch (JSONException ignored) {
            // The computer sent a complete replacement directory.
        }
        preferences.edit().putString(CATALOG, new JSONArray(entries.values()).toString()).apply();
    }

    private void addCatalog(JSONArray stored, Map<String, JSONObject> entries, boolean localOnly) {
        for (int i = 0; i < stored.length(); i++) {
            JSONObject entry = stored.optJSONObject(i);
            String id = entry == null ? null : entry.optString("id");
            if (validId(id) && (!localOnly || file(id).getBaseFile().isFile())) {
                entries.putIfAbsent(id, entry);
            }
        }
    }

    List<Entry> entries(List<ReminderHistory.Entry> reminders) {
        Map<String, Entry> byThread = new LinkedHashMap<>();
        try {
            JSONArray stored = new JSONArray(preferences.getString(CATALOG, "[]"));
            for (int i = 0; i < stored.length(); i++) {
                JSONObject entry = stored.optJSONObject(i);
                if (entry == null || !validId(entry.optString("id"))) {
                    continue;
                }
                String id = entry.optString("id");
                byThread.put(id, Entry.from(entry));
            }
        } catch (JSONException ignored) {
            // Reminder history still supplies entries when the catalog is unavailable.
        }
        String mostRecent = null;
        for (ReminderHistory.Entry reminder : reminders) {
            String id = threadFromEvent(reminder.id);
            if (id == null) {
                continue;
            }
            if (mostRecent == null) {
                mostRecent = id;
            }
            Entry previous = byThread.get(id);
            if (previous == null || reminder.receivedAt >= previous.activityAt) {
                String title = previous == null
                        ? reminder.title.replaceFirst("^(codex|claude) ", "") : previous.title;
                byThread.put(id, new Entry(id, title, previous == null ? "" : previous.project,
                        reminder.body, reminder.receivedAt, previous != null && previous.archived,
                        previous == null ? "" : previous.projectId, previous == null ? "" : previous.historyRevision));
            }
        }
        List<Entry> result = new ArrayList<>();
        for (Entry entry : byThread.values()) {
            String preview = preferences.getLong("preview_at:" + entry.id, 0) >= entry.activityAt
                    ? preferences.getString("preview:" + entry.id, entry.preview) : entry.preview;
            result.add(new Entry(entry.id, entry.title, entry.project, preview, entry.activityAt,
                    entry.archived, entry.projectId, entry.historyRevision));
        }
        String newest = mostRecent;
        result.sort(Comparator.comparingInt((Entry entry) -> entry.id.equals(newest) ? 0 : 1)
                .thenComparing(Comparator.comparingLong((Entry entry) -> entry.activityAt).reversed())
                .thenComparing(entry -> entry.id));
        return result;
    }

    JSONObject read(String thread) {
        synchronized (ThreadStore.class) {
            return readFile(thread);
        }
    }

    ProjectCatalog projects() {
        try {
            return new ProjectCatalog(new JSONObject(preferences.getString(PROJECTS, "{}")));
        } catch (JSONException ignored) {
            return new ProjectCatalog(new JSONObject());
        }
    }

    void saveProjects(JSONObject response) {
        if (response != null && response.optJSONArray("projects") != null
                && response.optJSONArray("threads") != null) {
            preferences.edit().putString(PROJECTS, response.toString()).apply();
        }
    }

    void updateThread(String id, Boolean archived, String projectId, String project) {
        updateThread(id, archived, projectId, project, null);
    }

    void renameThread(String id, String title) {
        updateThread(id, null, null, null, title);
    }

    void renameProject(String id, String name) {
        try {
            JSONObject catalog = new JSONObject(preferences.getString(PROJECTS, "{}"));
            JSONArray projects = catalog.optJSONArray("projects");
            JSONArray threads = catalog.optJSONArray("threads");
            for (int i = 0; projects != null && i < projects.length(); i++) {
                JSONObject project = projects.optJSONObject(i);
                if (project != null && id.equals(project.optString("id"))) {
                    project.put("name", name);
                }
            }
            for (int i = 0; threads != null && i < threads.length(); i++) {
                JSONObject thread = threads.optJSONObject(i);
                if (thread != null && id.equals(thread.optString("project_id"))) {
                    thread.put("project", name);
                }
            }
            saveProjects(catalog);
        } catch (JSONException ignored) {
            // The next sync loads the name from the project database.
        }
    }

    private void updateThread(String id, Boolean archived, String projectId, String project, String title) {
        try {
            JSONObject projects = new JSONObject(preferences.getString(PROJECTS, "{}"));
            JSONArray inbox = new JSONArray(preferences.getString(CATALOG, "[]"));
            SharedPreferences.Editor editor = preferences.edit();
            if (updateThread(projects.optJSONArray("threads"), id, archived, projectId, project, title)) {
                editor.putString(PROJECTS, projects.toString());
            }
            if (updateThread(inbox, id, archived, projectId, project, title)) {
                editor.putString(CATALOG, inbox.toString());
            }
            editor.apply();
        } catch (JSONException ignored) {
            // The next synchronization replaces the catalog.
        }
    }

    private boolean updateThread(JSONArray entries, String id, Boolean archived, String projectId, String project,
                                 String title) throws JSONException {
        for (int i = 0; entries != null && i < entries.length(); i++) {
            JSONObject entry = entries.optJSONObject(i);
            if (entry != null && id.equals(entry.optString("id"))) {
                if (archived != null) {
                    entry.put("archived", archived);
                }
                if (projectId != null) {
                    entry.put("project_id", projectId).put("project", project);
                }
                if (title != null) {
                    entry.put("title", title);
                }
                return true;
            }
        }
        return false;
    }

    boolean hideArchived() {
        return preferences.getBoolean(HIDE_ARCHIVED, false);
    }

    boolean hasRevision(Entry entry) {
        return entry.historyRevision.equals(preferences.getString("history_rev:" + entry.id, ""))
                && file(entry.id).getBaseFile().isFile();
    }

    void recordRevision(String thread, String revision) {
        preferences.edit().putString("history_rev:" + thread, revision).apply();
    }

    private JSONObject readFile(String thread) {
        if (!validId(thread)) {
            return null;
        }
        try {
            return new JSONObject(new String(file(thread).readFully(), StandardCharsets.UTF_8));
        } catch (IOException | JSONException ignored) {
            return null;
        }
    }

    JSONObject save(String thread, JSONObject snapshot) throws IOException {
        synchronized (ThreadStore.class) {
            if (!validId(thread) || snapshot.optJSONArray("turns") == null) {
                throw new IOException("Invalid conversation snapshot");
            }
            JSONObject previous = readFile(thread);
            if (previous != null && previous.optLong("modified_at") > snapshot.optLong("modified_at")) {
                return previous;
            }
            if (!directory.isDirectory() && !directory.mkdirs()) {
                throw new IOException("Cannot create conversation storage");
            }
            AtomicFile target = file(thread);
            FileOutputStream stream = target.startWrite();
            try {
                stream.write(snapshot.toString().getBytes(StandardCharsets.UTF_8));
                target.finishWrite(stream);
            } catch (IOException error) {
                target.failWrite(stream);
                throw error;
            }
            String preview = preview(snapshot.optJSONArray("turns"));
            SharedPreferences.Editor metadata = preferences.edit()
                    .putString("history_rev:" + thread, snapshot.optString("cache_revision", snapshot.optString("revision")));
            if (preview != null) {
                metadata.putString("preview:" + thread, preview)
                        .putLong("preview_at:" + thread, System.currentTimeMillis());
            }
            metadata.apply();
            return snapshot;
        }
    }

    private AtomicFile file(String thread) {
        return new AtomicFile(new File(directory, thread + ".json"));
    }

    private static String preview(JSONArray turns) {
        for (int i = turns.length() - 1; i >= 0; i--) {
            JSONObject turn = turns.optJSONObject(i);
            JSONArray messages = turn == null ? null : turn.optJSONArray("messages");
            for (int j = messages == null ? -1 : messages.length() - 1; j >= 0; j--) {
                JSONObject message = messages.optJSONObject(j);
                String value = message == null ? "" : message.optString("text").trim();
                if (!value.isEmpty()) {
                    return value.substring(0, value.offsetByCodePoints(0,
                            Math.min(180, value.codePointCount(0, value.length()))));
                }
            }
        }
        return null;
    }

    static final class Entry {
        final String id;
        final String title;
        final String project;
        final String preview;
        final long activityAt;
        final boolean archived;
        final String projectId;
        final String historyRevision;

        Entry(String id, String title, String project, String preview, long activityAt,
              boolean archived, String projectId, String historyRevision) {
            this.id = id;
            this.title = title;
            this.project = project;
            this.preview = preview;
            this.activityAt = activityAt;
            this.archived = archived;
            this.projectId = projectId;
            this.historyRevision = historyRevision;
        }

        static Entry from(JSONObject data) {
            return new Entry(data.optString("id"), data.optString("title"), data.optString("project"),
                    data.optString("preview"), data.optLong("activity_at"), data.optBoolean("archived"),
                    data.optString("project_id"), data.optString("history_revision"));
        }
    }

    static final class ProjectCatalog {
        final String revision;
        final Map<String, String> names = new LinkedHashMap<>();
        final List<Entry> threads = new ArrayList<>();

        ProjectCatalog(JSONObject data) {
            revision = data.optString("revision");
            JSONArray projects = data.optJSONArray("projects");
            for (int i = 0; projects != null && i < projects.length(); i++) {
                JSONObject project = projects.optJSONObject(i);
                if (project != null && !project.optString("id").isEmpty()) {
                    names.put(project.optString("id"), project.optString("name"));
                }
            }
            JSONArray entries = data.optJSONArray("threads");
            for (int i = 0; entries != null && i < entries.length(); i++) {
                JSONObject entry = entries.optJSONObject(i);
                if (entry != null && validId(entry.optString("id"))) {
                    threads.add(Entry.from(entry));
                }
            }
        }

        Entry find(String thread) {
            for (Entry entry : threads) {
                if (entry.id.equals(thread)) {
                    return entry;
                }
            }
            return null;
        }
    }
}
