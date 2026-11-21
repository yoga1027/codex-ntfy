package local.codexntfy.opush;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Locale;

final class AccountUsage {
    private static final DateTimeFormatter CLOCK = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm", Locale.ROOT)
            .withZone(ZoneId.of("Asia/Shanghai"));

    static String format(JSONObject data) {
        StringBuilder report = new StringBuilder();
        appendAccount(report, "Codex", data.optJSONObject("codex"));
        report.append("\n\n");
        appendAccount(report, "Claude", data.optJSONObject("claude"));
        report.append("\n\n查询时间  ").append(time(data.optLong("queried_at"))).append("（北京时间）");
        return report.toString();
    }

    private static void appendAccount(StringBuilder report, String name, JSONObject account) {
        report.append(name);
        if (account == null || account.optBoolean("unavailable")) {
            report.append("\n电脑客户端暂未返回额度");
            return;
        }
        if (account.has("observed_at")) {
            report.append(" · 电脑记录于 ").append(time(account.optLong("observed_at")));
        }
        appendWindow(report, "5 小时", account.optJSONObject("300"));
        appendWindow(report, "7 天", account.optJSONObject("10080"));
        if (account.isNull("card_count")) {
            report.append("\n重置卡  客户端未提供");
            return;
        }
        int count = account.optInt("card_count");
        report.append("\n重置卡  ").append(count).append(" 张");
        JSONArray cards = account.optJSONArray("expiries");
        int known = cards == null ? 0 : cards.length();
        for (int i = 0; i < known; i++) {
            report.append("\n  第 ").append(i + 1).append(" 张 · ");
            if (cards.isNull(i)) report.append("无到期日");
            else report.append(time(cards.optLong(i))).append(" 到期");
        }
        if (known < count) report.append("\n  另有 ").append(count - known).append(" 张未提供到期明细");
    }

    private static void appendWindow(StringBuilder report, String name, JSONObject window) {
        report.append("\n").append(name).append("  ");
        if (window == null || window.isNull("remaining")) {
            report.append("暂未提供");
            return;
        }
        report.append("剩余 ").append(window.optString("remaining")).append("%");
        if (window.isNull("reset")) report.append(" · 重置时间未提供");
        else report.append(" · ").append(time(window.optLong("reset"))).append(" 重置");
    }

    private static String time(long seconds) {
        return seconds > 0 ? CLOCK.format(Instant.ofEpochSecond(seconds)) : "—";
    }
}
