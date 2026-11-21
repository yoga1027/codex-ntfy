package local.codexntfy.opush;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

final class ReminderHistory {
    static final String PREFS = "tailscale_delivered";
    // Delivery keys from the server contain colons. Keep this key outside that namespace.
    static final String KEY = "history_v1";
    private static final int LIMIT = 100;

    private ReminderHistory() {}

    static List<Entry> read(String encoded) {
        JSONArray saved = parse(encoded);
        List<Entry> entries = new ArrayList<>(saved.length());
        for (int i = 0; i < saved.length(); i++) {
            JSONObject item = saved.optJSONObject(i);
            if (item != null) {
                try {
                    entries.add(new Entry(item.getString("id"), item.getString("title"),
                            item.getString("body"), item.getLong("received_at_ms")));
                } catch (JSONException ignored) {
                    // A malformed record should not hide the rest of the history.
                }
            }
        }
        return entries;
    }

    static String prepend(String previous, Entry reminder) throws JSONException {
        JSONArray saved = parse(previous);
        JSONArray next = new JSONArray().put(new JSONObject()
                .put("id", reminder.id)
                .put("title", reminder.title)
                .put("body", reminder.body)
                .put("received_at_ms", reminder.receivedAt));
        for (int i = 0; i < saved.length() && next.length() < LIMIT; i++) {
            JSONObject item = saved.optJSONObject(i);
            if (item != null && !reminder.id.equals(item.optString("id"))) {
                next.put(item);
            }
        }
        return next.toString();
    }

    private static JSONArray parse(String encoded) {
        try {
            return encoded == null ? new JSONArray() : new JSONArray(encoded);
        } catch (JSONException ignored) {
            return new JSONArray();
        }
    }

    static final class Entry {
        final String id;
        final String title;
        final String body;
        final long receivedAt;

        Entry(String id, String title, String body, long receivedAt) {
            this.id = id;
            this.title = title;
            this.body = body;
            this.receivedAt = receivedAt;
        }
    }
}
