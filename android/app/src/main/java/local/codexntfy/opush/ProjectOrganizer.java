package local.codexntfy.opush;

import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.text.InputFilter;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.Set;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/** Phone-local layout for the desktop project list and each project's conversation list. */
final class ProjectOrganizer {
    static final String PROJECTS = "projects";
    private final Context context;
    private final SharedPreferences preferences;

    ProjectOrganizer(Context context) {
        this.context = context;
        preferences = context.getSharedPreferences("project_organization", Context.MODE_PRIVATE);
    }

    static String conversations(String project) {
        return "conversations:" + project;
    }

    String projectName(String project, String fallback) {
        return preferences.getString("alias:" + project, fallback);
    }

    void renameLocalProject(String project, String name) {
        preferences.edit().putString("alias:" + project, name).apply();
    }

    private static final String DEFAULT_GROUP = "default";

    List<Section> sections(String scope, Map<String, String> labels, Map<String, Long> activity) {
        Board board = board(scope);
        String before = board.data.toString();
        List<Section> sections = board.sections(labels, activity);
        if (!before.equals(board.data.toString())) persist(scope, board);
        return sections;
    }

    boolean pinned(String scope, String id) {
        return board(scope).pins.has(id);
    }

    void togglePin(String scope, String id, Runnable refresh) {
        Board board = board(scope);
        if (board.pins.has(id)) board.pins.remove(id);
        else put(board.pins, id, System.currentTimeMillis());
        save(scope, board, refresh);
    }

    void bindItem(View view, String scope, String id, Section section, Runnable refresh, Runnable renameProject) {
        view.setOnLongClickListener(ignored -> {
            Board board = board(scope);
            List<String> actions = new ArrayList<>();
            actions.add(context.getString(board.pins.has(id) ? R.string.organization_unpin : R.string.organization_pin));
            actions.add(context.getString(R.string.organization_classify));
            actions.add("排序");
            if (renameProject != null) actions.add(context.getString(R.string.organization_rename_project));
            new AlertDialog.Builder(context).setItems(actions.toArray(new String[0]), (dialog, index) -> {
                if (index == 0) togglePin(scope, id, refresh);
                else if (index == 1) chooseGroup(scope, board, id, refresh);
                else if (index == 2) {
                    List<String> peers = new ArrayList<>(section.ids);
                    peers.removeIf(peer -> board.pins.has(peer) != board.pins.has(id));
                    sort(scope, "order", id, peers, section.labels, refresh);
                } else if (renameProject != null) renameProject.run();
            }).setNegativeButton(android.R.string.cancel, null).show();
            return true;
        });
    }

    private void sort(String scope, String key, String id, List<String> peers, Map<String, String> labels,
                      Runnable refresh) {
        String[] choices = new String[peers.size()];
        for (int i = 0; i < choices.length; i++) choices[i] = (i + 1) + "  ·  " + labels.get(peers.get(i));
        new AlertDialog.Builder(context).setTitle("移到第几位")
                .setSingleChoiceItems(choices, peers.indexOf(id), (dialog, index) -> {
                    Board board = board(scope);
                    List<String> order = board.list(key);
                    // Keep arrivals and other peer moves received while the picker was open.
                    String target = peers.get(index);
                    if (!id.equals(target) && order.contains(id) && order.contains(target)) {
                        boolean after = peers.indexOf(id) < index;
                        order.remove(id);
                        order.add(order.indexOf(target) + (after ? 1 : 0), id);
                        put(board.data, key, new JSONArray(order));
                    }
                    dialog.dismiss();
                    save(scope, board, refresh);
                }).setNegativeButton(android.R.string.cancel, null).show();
    }

    void bindSection(View header, String scope, Section section, Runnable refresh) {
        header.setOnLongClickListener(view -> {
            Board board = board(scope);
            new AlertDialog.Builder(context).setTitle(section.title).setItems(new String[]{"重命名", "排序"},
                    (dialog, index) -> {
                        if (index == 0) editGroup(scope, board, section.group, null, refresh);
                        else {
                            Map<String, String> labels = new LinkedHashMap<>();
                            for (String group : board.list("categories")) labels.put(group, board.names.optString(group));
                            sort(scope, "categories", section.group, new ArrayList<>(labels.keySet()), labels, refresh);
                        }
                    }).setNegativeButton(android.R.string.cancel, null).show();
            return true;
        });
    }

    private void chooseGroup(String scope, Board board, String id, Runnable refresh) {
        List<String> groups = board.list("categories");
        List<String> labels = new ArrayList<>();
        for (String key : groups) labels.add(board.names.optString(key));
        int create = labels.size();
        labels.add(context.getString(R.string.organization_create));
        if (!board.defaultGroup.equals(board.group(id))) labels.add(context.getString(R.string.organization_clear));
        new AlertDialog.Builder(context).setTitle(R.string.organization_classify)
                .setItems(labels.toArray(new String[0]), (dialog, index) -> {
                    if (index == create) {
                        editGroup(scope, board, "", id, refresh);
                    } else {
                        Board current = board(scope);
                        if (index < create) put(current.members, id, groups.get(index));
                        else current.members.remove(id);
                        save(scope, current, refresh);
                    }
                }).setNegativeButton(android.R.string.cancel, null).show();
    }

    private void editGroup(String scope, Board board, String group, String member, Runnable refresh) {
        boolean create = group.isEmpty();
        EditText field = new EditText(context);
        field.setSingleLine();
        field.setHint(R.string.organization_name);
        field.setFilters(new InputFilter[]{new InputFilter.LengthFilter(40)});
        field.setText(create ? "" : board.names.optString(group));
        field.selectAll();
        LinearLayout layout = new LinearLayout(context);
        int inset = (int) (24 * context.getResources().getDisplayMetrics().density);
        layout.setPadding(inset, inset / 2, inset, 0);
        layout.addView(field, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        AlertDialog dialog = new AlertDialog.Builder(context)
                .setTitle(create ? R.string.organization_create : R.string.organization_rename)
                .setView(layout).setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(android.R.string.ok, null).create();
        dialog.show();
        dialog.getButton(DialogInterface.BUTTON_POSITIVE).setOnClickListener(view -> {
            String title = field.getText().toString().trim().replaceAll("\\s+", " ");
            if (title.isEmpty()) {
                field.setError(context.getString(R.string.organization_name_required));
                return;
            }
            Board current = board(scope);
            for (Iterator<String> keys = current.names.keys(); keys.hasNext();) {
                String other = keys.next();
                if (!other.equals(group) && title.equals(current.names.optString(other))) {
                    field.setError(context.getString(R.string.organization_duplicate_name));
                    return;
                }
            }
            String key = create ? UUID.randomUUID().toString() : group;
            put(current.names, key, title);
            if (member != null) {
                put(current.members, member, key);
            }
            dialog.dismiss();
            save(scope, current, refresh);
        });
    }

    private Board board(String scope) {
        try {
            return new Board(new JSONObject(preferences.getString(scope, "{}")));
        } catch (JSONException ignored) {
            return new Board(new JSONObject());
        }
    }

    private void save(String scope, Board board, Runnable refresh) {
        persist(scope, board);
        refresh.run();
    }

    private void persist(String scope, Board board) {
        preferences.edit().putString(scope, board.data.toString()).apply();
    }

    private static void put(JSONObject data, String key, Object value) {
        try {
            data.put(key, value);
        } catch (JSONException error) {
            throw new IllegalArgumentException("Invalid project organization data", error);
        }
    }

    static final class Section {
        final String group;
        final String title;
        final List<String> ids;

        final Map<String, String> labels;

        Section(String group, String title, List<String> ids, Map<String, String> labels) {
            this.labels = labels;
            this.group = group;
            this.title = title;
            this.ids = ids;
        }
    }

    static final class Board {
        final String defaultGroup;
        final JSONObject data;
        final JSONObject activity;
        final JSONObject titles;
        final JSONObject names;
        final JSONObject members;
        final JSONObject pins;

        Board(JSONObject data) {
            this.data = data;
            activity = object(data, "activity");
            titles = object(data, "titles");
            names = object(data, "names");
            members = object(data, "members");
            pins = object(data, "pins");
            String key = data.optString("defaultGroup", DEFAULT_GROUP);
            if (!names.has(key)) {
                // Reuse a category previously created with this name when upgrading from 0.13.
                for (Iterator<String> it = names.keys(); it.hasNext();) {
                    String existing = it.next();
                    if ("默认".equals(names.optString(existing))) {
                        key = existing;
                        break;
                    }
                }
                if (!names.has(key)) put(names, key, "默认");
            }
            defaultGroup = key;
            put(data, "defaultGroup", key);
        }

        List<Section> sections(Map<String, String> labels, Map<String, Long> activityTimes) {
            List<String> order = list("order");
            Set<String> known = new HashSet<>(order);
            List<String> promoted = new ArrayList<>();
            for (Map.Entry<String, String> entry : labels.entrySet()) {
                String id = entry.getKey();
                boolean fresh = known.add(id);
                if (fresh) order.add(id);
                Long updated = activityTimes.get(id);
                if (updated == null) continue;  // Projects keep their saved positions.
                boolean renamed = titles.has(id) && !entry.getValue().equals(titles.optString(id));
                if (fresh || updated > activity.optLong(id) || renamed) promoted.add(id);
                put(activity, id, Math.max(updated, activity.optLong(id)));
                put(titles, id, entry.getValue());
            }
            promoted.sort(Comparator.comparingLong((String id) -> activityTimes.get(id)).reversed());
            order.removeAll(new HashSet<>(promoted));
            order.addAll(0, promoted);
            put(data, "order", new JSONArray(order));

            Map<String, List<String>> groups = new LinkedHashMap<>();
            for (String id : order) {
                if (labels.containsKey(id)) groups.computeIfAbsent(group(id), unused -> new ArrayList<>()).add(id);
            }
            List<String> categories = list("categories");
            for (String key : groups.keySet()) if (!categories.contains(key)) categories.add(key);
            for (Iterator<String> it = names.keys(); it.hasNext();) {
                String key = it.next();
                if (!categories.contains(key)) categories.add(key);
            }
            put(data, "categories", new JSONArray(categories));
            List<Section> sections = new ArrayList<>();
            for (String key : categories) {
                List<String> ids = groups.get(key);
                if (ids == null) continue;
                ids.sort((left, right) -> Boolean.compare(pins.has(right), pins.has(left)));
                sections.add(new Section(key, names.optString(key), ids, labels));
            }
            return sections;
        }

        List<String> list(String key) {
            JSONArray values = data.optJSONArray(key);
            List<String> result = new ArrayList<>();
            for (int i = 0; values != null && i < values.length(); i++) result.add(values.optString(i));
            return result;
        }

        String group(String id) {
            String group = members.optString(id);
            return names.has(group) ? group : defaultGroup;
        }

        private static JSONObject object(JSONObject data, String key) {
            JSONObject value = data.optJSONObject(key);
            if (value == null) {
                value = new JSONObject();
                put(data, key, value);
            }
            return value;
        }
    }
}
