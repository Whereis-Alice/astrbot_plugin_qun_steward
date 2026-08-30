/* 群务管家 WebUI —— 纯原生 JS，无外部依赖。
 * 数据全部来自插件自身的 Web API（web/api.py），通过 AstrBot 注入的
 * window.AstrBotPluginPage 访问，因此不需要手动处理鉴权与路径前缀。
 */
(function () {
  "use strict";

  var page = window.AstrBotPluginPage || null;

  var state = {
    view: "overview",
    version: "",
    displayName: "群务管家",
    fields: [],
    groups: [],
    groupIndex: {},
    groupKeyword: "",
    currentGroup: "",
    audit: { limit: 50, offset: 0, group_id: "", action: "", keyword: "" }
  };

  /* ------------------------------------------------------------------ 工具 */

  function t(key, fallback) {
    if (page && typeof page.t === "function") {
      try {
        var value = page.t(key, fallback);
        if (value && value !== key) return value;
      } catch (err) { /* 忽略 i18n 异常，使用回退文案 */ }
    }
    return fallback;
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "dataset") Object.keys(value).forEach(function (d) { node.dataset[d] = value[d]; });
      else if (key.slice(0, 2) === "on" && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "value") node.value = value;
      else if (key === "checked") node.checked = !!value;
      else node.setAttribute(key, value === true ? "" : String(value));
    });
    append(node, children);
    return node;
  }

  function append(parent, children) {
    if (children === null || children === undefined || children === false) return parent;
    if (Array.isArray(children)) {
      children.forEach(function (child) { append(parent, child); });
      return parent;
    }
    if (children instanceof Node) parent.appendChild(children);
    else parent.appendChild(document.createTextNode(String(children)));
    return parent;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function toast(message, kind) {
    var host = document.getElementById("toast-host");
    if (!host) return;
    var item = el("div", { class: "toast " + (kind || ""), text: String(message) });
    host.appendChild(item);
    setTimeout(function () {
      item.style.opacity = "0";
      setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 260);
    }, kind === "err" ? 5200 : 2600);
  }

  function plain(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback || "-";
    return String(value);
  }

  /* -------------------------------------------------------------- 接口调用 */

  function unwrap(payload, depth) {
    depth = depth || 0;
    if (!payload || typeof payload !== "object" || depth > 3) return payload;
    if (Object.prototype.hasOwnProperty.call(payload, "ok")) {
      if (!payload.ok) throw new Error(payload.error || t("common.requestFailed", "接口返回失败"));
      return payload.data;
    }
    if (Object.prototype.hasOwnProperty.call(payload, "data")) return unwrap(payload.data, depth + 1);
    return payload;
  }

  function apiGet(endpoint, params) {
    if (!page || typeof page.apiGet !== "function") {
      return Promise.reject(new Error(t("common.noSdk", "未检测到 AstrBot 页面运行环境")));
    }
    return Promise.resolve(page.apiGet(endpoint, params || {})).then(function (res) { return unwrap(res); });
  }

  function apiPost(endpoint, body) {
    if (!page || typeof page.apiPost !== "function") {
      return Promise.reject(new Error(t("common.noSdk", "未检测到 AstrBot 页面运行环境")));
    }
    return Promise.resolve(page.apiPost(endpoint, body || {})).then(function (res) { return unwrap(res); });
  }

  /* ------------------------------------------------------------ 表单控件 */

  function listToText(value) {
    if (Array.isArray(value)) return value.join("\n");
    if (value === null || value === undefined) return "";
    return String(value);
  }

  function textToList(text) {
    return String(text || "")
      .split(/[\r\n]+/)
      .map(function (line) { return line.trim(); })
      .filter(function (line) { return line.length > 0; });
  }

  /** 按字段元信息生成控件；返回 {node, read} */
  function makeControl(meta, value) {
    var type = String(meta.type || "string");
    var options = Array.isArray(meta.options) ? meta.options : [];
    var input;

    if (type === "bool") {
      input = el("input", { type: "checkbox", checked: !!value });
      return {
        node: el("label", { class: "switch" }, [input, el("span", { text: value ? t("common.on", "开启") : t("common.off", "关闭") })]),
        read: function () { return !!input.checked; },
        onChange: function (fn) { input.addEventListener("change", fn); }
      };
    }
    if (options.length) {
      input = el("select", {});
      options.forEach(function (opt) {
        input.appendChild(el("option", { value: String(opt), text: String(opt), selected: String(opt) === String(value) }));
      });
      if (options.map(String).indexOf(String(value)) < 0) input.value = String(options[0]);
      return { node: input, read: function () { return input.value; }, onChange: function (fn) { input.addEventListener("change", fn); } };
    }
    if (type === "int" || type === "float") {
      input = el("input", { type: "number", step: type === "int" ? "1" : "any", value: value === null || value === undefined ? "" : String(value) });
      return {
        node: input,
        read: function () {
          if (input.value === "") return 0;
          var num = type === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
          return isNaN(num) ? 0 : num;
        },
        onChange: function (fn) { input.addEventListener("input", fn); }
      };
    }
    if (type === "list") {
      input = el("textarea", { value: listToText(value), placeholder: t("common.onePerLine", "每行一项") });
      return { node: input, read: function () { return textToList(input.value); }, onChange: function (fn) { input.addEventListener("input", fn); } };
    }
    if (type === "text") {
      input = el("textarea", { value: value === null || value === undefined ? "" : String(value) });
      return { node: input, read: function () { return input.value; }, onChange: function (fn) { input.addEventListener("input", fn); } };
    }
    input = el("input", { type: "text", value: value === null || value === undefined ? "" : String(value) });
    return { node: input, read: function () { return input.value; }, onChange: function (fn) { input.addEventListener("input", fn); } };
  }

  /** 渲染字段表单；collect() 只返回被改动过的字段 */
  function buildFieldForm(fields, values, overrides) {
    var wrap = el("div", { class: "grid cols-2" });
    var controls = {};
    var initial = {};
    var overrideSet = {};
    (overrides ? Object.keys(overrides) : []).forEach(function (key) { overrideSet[key] = true; });

    fields.forEach(function (meta) {
      var value = values && Object.prototype.hasOwnProperty.call(values, meta.field) ? values[meta.field] : meta.default;
      var control = makeControl(meta, value);
      controls[meta.field] = control;
      initial[meta.field] = JSON.stringify(control.read());
      var cls = "field" + (overrideSet[meta.field] ? " overridden" : "");
      wrap.appendChild(el("div", { class: cls }, [
        el("span", { class: "field-label", text: meta.label || meta.field }),
        control.node,
        meta.hint ? el("span", { class: "field-hint", text: meta.hint }) : null
      ]));
    });

    return {
      node: wrap,
      collect: function () {
        var changes = {};
        Object.keys(controls).forEach(function (field) {
          var current = controls[field].read();
          if (JSON.stringify(current) !== initial[field]) changes[field] = current;
        });
        return changes;
      }
    };
  }

  /* ------------------------------------------------------------ 页面骨架 */

  function VIEWS() {
    return [
      { id: "overview", icon: "◎", label: t("nav.overview", "总览"), desc: t("nav.overviewDesc", "插件运行状态与最近操作统计"), render: renderOverview },
      { id: "groups", icon: "☰", label: t("nav.groups", "群配置"), desc: t("nav.groupsDesc", "按群覆写配置；未覆写的项目自动跟随默认模板"), render: renderGroups },
      { id: "defaults", icon: "⚙", label: t("nav.defaults", "默认模板"), desc: t("nav.defaultsDesc", "所有群的基础配置，改动会影响未单独覆写的群"), render: renderDefaults },
      { id: "perms", icon: "🔑", label: t("nav.perms", "权限矩阵"), desc: t("nav.permsDesc", "每条指令允许的最低使用者身份"), render: renderPerms },
      { id: "words", icon: "🛡", label: t("nav.words", "违禁词"), desc: t("nav.wordsDesc", "查看某个群生效的违禁词与处理方式"), render: renderWords },
      { id: "audit", icon: "🧾", label: t("nav.audit", "操作日志"), desc: t("nav.auditDesc", "所有群管操作的可追溯记录"), render: renderAudit },
      { id: "joins", icon: "📥", label: t("nav.joins", "待审进群"), desc: t("nav.joinsDesc", "仍在等待处理的入群申请（在群内用「批准 / 驳回」处理）"), render: renderJoins },
      { id: "album", icon: "🖼", label: t("nav.album", "群相册"), desc: t("nav.albumDesc", "相册列表、随机图关键词与相关开关"), render: renderAlbum },
      { id: "settings", icon: "🧩", label: t("nav.settings", "全局设置"), desc: t("nav.settingsDesc", "投票、安全、审计、相册与字体等全局项"), render: renderSettings }
    ];
  }

  function currentView() {
    var all = VIEWS();
    for (var i = 0; i < all.length; i += 1) if (all[i].id === state.view) return all[i];
    return all[0];
  }

  function renderNav() {
    var nav = clear(document.getElementById("nav"));
    VIEWS().forEach(function (view) {
      nav.appendChild(el("button", {
        type: "button",
        class: view.id === state.view ? "active" : "",
        onclick: function () { navigate(view.id); }
      }, [el("span", { class: "nav-icon", text: view.icon }), el("span", { text: view.label })]));
    });
  }

  function navigate(id) {
    state.view = id;
    if (window.location.hash !== "#" + id) window.location.hash = "#" + id;
    renderNav();
    renderMain();
  }

  function renderMain() {
    var view = currentView();
    var main = clear(document.getElementById("main"));
    var actions = el("div", { class: "page-actions" });
    main.appendChild(el("div", { class: "page-head" }, [
      el("div", {}, [
        el("h1", { class: "page-title", text: view.label }),
        el("p", { class: "page-desc", text: view.desc })
      ]),
      actions
    ]));
    var body = el("div", {});
    main.appendChild(body);
    body.appendChild(el("div", { class: "loading", text: t("common.loading", "加载中…") }));
    Promise.resolve(view.render(body, actions)).catch(function (err) {
      clear(body).appendChild(el("div", { class: "card" }, [
        el("p", { class: "card-note", text: t("common.loadFailed", "数据加载失败：") + (err && err.message ? err.message : err) }),
        el("button", { class: "btn", type: "button", text: t("common.retry", "重试"), onclick: renderMain })
      ]));
    });
  }

  function section(title, children, note) {
    return el("div", { class: "card" }, [
      title ? el("h2", { class: "card-title", text: title }) : null,
      note ? el("p", { class: "card-note", text: note }) : null,
      children
    ]);
  }

  function statCard(label, value, hint) {
    return el("div", { class: "stat" }, [
      el("div", { class: "stat-label", text: label }),
      el("div", { class: "stat-value", text: String(value) }),
      hint ? el("div", { class: "stat-hint", text: hint }) : null
    ]);
  }

  function emptyBox(text) {
    return el("div", { class: "empty", text: text || t("common.empty", "暂无数据") });
  }

  function table(headers, rows) {
    var thead = el("thead", {}, el("tr", {}, headers.map(function (h) { return el("th", { text: h }); })));
    var tbody = el("tbody", {}, rows.map(function (cells) {
      return el("tr", {}, cells.map(function (cell) {
        return el("td", {}, cell instanceof Node ? cell : el("span", { text: plain(cell) }));
      }));
    }));
    return el("div", { class: "table-wrap" }, el("table", {}, [thead, tbody]));
  }

  /* ---------------------------------------------------------------- 总览 */

  function renderOverview(body, actions) {
    return apiGet("overview").then(function (data) {
      state.version = (data.plugin && data.plugin.version) || state.version;
      state.displayName = (data.plugin && data.plugin.display_name) || state.displayName;
      paintBrand();
      clear(body);

      var groups = data.groups || {};
      var audit = data.audit || {};
      body.appendChild(el("div", { class: "grid cols-4" }, [
        statCard(t("overview.groups", "已接入群"), groups.total || 0, t("overview.groupsHint", "机器人当前可见的群聊")),
        statCard(t("overview.customized", "单独配置的群"), groups.customized || 0, t("overview.customizedHint", "其余群跟随默认模板")),
        statCard(t("overview.botAdmin", "拥有管理权"), groups.bot_admin || 0, t("overview.botAdminHint", "机器人为管理员或群主")),
        statCard(t("overview.pending", "待审进群"), data.pending_joins || 0, t("overview.pendingHint", "群内发送「待审进群」处理"))
      ]));

      body.appendChild(el("div", { class: "grid cols-4", style: "margin-top:12px" }, [
        statCard(t("overview.auditTotal", "操作日志"), audit.total || 0, audit.enabled ? t("overview.auditOn", "记录已开启") : t("overview.auditOff", "记录已关闭")),
        statCard(t("overview.backend", "协议端"), plain(data.backend, t("common.unknown", "未探测")), t("overview.backendHint", "napcat / llbot / snowluma 自动识别")),
        statCard(t("overview.undo", "撤销窗口"), (data.undo_window || 0) + "s", t("overview.undoHint", "群内发送「撤销」可回滚")),
        statCard(t("overview.curfew", "宵禁任务"), (data.curfew || []).length, t("overview.curfewHint", "已开启宵禁的群数量"))
      ]));

      var recent = (audit.recent && audit.recent.by_action) || [];
      var max = recent.reduce(function (acc, item) { return Math.max(acc, item.count || 0); }, 0) || 1;
      var barsNote = t("overview.recentNote", "统计近 7 天各类操作次数，帮助快速发现异常。");
      body.appendChild(section(
        t("overview.recentTitle", "最近操作分布"),
        recent.length
          ? el("div", { class: "bars" }, recent.slice(0, 12).map(function (item) {
              return el("div", { class: "bar-row" }, [
                el("span", { text: item.label || item.action }),
                el("div", { class: "bar-track" }, el("div", { class: "bar-fill", style: "width:" + Math.round((item.count / max) * 100) + "%" })),
                el("span", { class: "bar-value", text: String(item.count) })
              ]);
            }))
          : emptyBox(t("overview.recentEmpty", "近 7 天还没有操作记录")),
        barsNote
      ));

      var curfew = data.curfew || [];
      body.appendChild(section(
        t("overview.curfewTitle", "宵禁概览"),
        curfew.length
          ? table([t("common.group", "群号"), t("overview.curfewRange", "宵禁时段"), t("common.status", "状态")],
              curfew.map(function (item) {
                return [
                  item.group_id,
                  plain(item.start_time, "?") + " - " + plain(item.end_time, "?"),
                  el("span", { class: "tag " + (item.active ? "warn" : "ok"), text: item.active ? t("overview.curfewActive", "禁言中") : t("overview.curfewIdle", "空闲") })
                ];
              }))
          : emptyBox(t("overview.curfewEmpty", "当前没有群开启宵禁")),
        t("overview.curfewNote", "群内发送「开启宵禁 23:00 07:00」即可启用。")
      ));

      body.appendChild(el("p", { class: "card-note", style: "margin-top:12px", text: t("overview.generated", "数据生成时间：") + plain(data.generated_at) }));

      actions.appendChild(el("button", { class: "btn", type: "button", text: t("common.refresh", "刷新"), onclick: renderMain }));
    });
  }

  /* -------------------------------------------------------------- 群配置 */

  function loadFields() {
    if (state.fields.length) return Promise.resolve(state.fields);
    return apiGet("fields").then(function (fields) {
      state.fields = fields || [];
      return state.fields;
    });
  }

  function loadGroups(force) {
    if (state.groups.length && !force) return Promise.resolve(state.groups);
    return apiGet("groups", force ? { force: "1" } : {}).then(function (groups) {
      state.groups = groups || [];
      state.groupIndex = {};
      state.groups.forEach(function (item) { state.groupIndex[String(item.group_id)] = item; });
      return state.groups;
    });
  }

  function groupCard(item, onPick) {
    var gid = String(item.group_id);
    return el("button", {
      type: "button",
      class: "group-card" + (gid === state.currentGroup ? " active" : ""),
      onclick: function () { onPick(gid); }
    }, [
      el("img", { class: "group-avatar", src: item.avatar || "", alt: "", loading: "lazy", onerror: function () { this.style.visibility = "hidden"; } }),
      el("div", { class: "group-meta" }, [
        el("div", { class: "group-name", text: plain(item.group_name, gid) }),
        el("div", { class: "group-sub", text: gid + " · " + plain(item.bot_role_label, "-") + " · " + (item.member_count || 0) + t("common.members", " 人") }),
        item.customized ? el("span", { class: "tag warn", text: t("groups.customized", "已单独配置") }) : el("span", { class: "tag", text: t("groups.followDefault", "跟随默认") })
      ])
    ]);
  }

  function groupPicker(host, onPick) {
    var listHost = el("div", { class: "group-list" });
    var search = el("input", {
      type: "search",
      placeholder: t("groups.search", "搜索群号或群名"),
      value: state.groupKeyword,
      oninput: function () { state.groupKeyword = this.value.trim(); paint(); }
    });

    function paint() {
      clear(listHost);
      var kw = state.groupKeyword.toLowerCase();
      var matched = state.groups.filter(function (item) {
        if (!kw) return true;
        return String(item.group_id).indexOf(kw) >= 0 || String(item.group_name || "").toLowerCase().indexOf(kw) >= 0;
      });
      if (!matched.length) { listHost.appendChild(emptyBox(t("groups.noMatch", "没有匹配的群"))); return; }
      matched.slice(0, 60).forEach(function (item) { listHost.appendChild(groupCard(item, onPick)); });
      if (matched.length > 60) listHost.appendChild(el("div", { class: "empty", text: t("groups.tooMany", "仅显示前 60 个，请用搜索缩小范围") }));
    }

    paint();
    host.appendChild(el("div", { class: "toolbar" }, [
      el("div", { class: "grow" }, search),
      el("button", { class: "btn small", type: "button", text: t("common.reloadGroups", "重新拉取群列表"), onclick: function () {
        loadGroups(true).then(function () { paint(); toast(t("common.done", "已更新"), "ok"); }).catch(function (e) { toast(e.message, "err"); });
      } })
    ]));
    host.appendChild(listHost);
    return { repaint: paint };
  }

  function renderGroups(body, actions) {
    return Promise.all([loadFields(), loadGroups(false)]).then(function () {
      clear(body);
      var pickerHost = el("div", { class: "card" });
      var detailHost = el("div", {});
      body.appendChild(pickerHost);
      body.appendChild(detailHost);

      var picker = groupPicker(pickerHost, function (gid) {
        state.currentGroup = gid;
        picker.repaint();
        showGroupDetail(detailHost, gid);
      });

      if (state.currentGroup && state.groupIndex[state.currentGroup]) showGroupDetail(detailHost, state.currentGroup);
      else detailHost.appendChild(section(null, emptyBox(t("groups.pick", "先在上方选择一个群"))));

      actions.appendChild(el("button", {
        class: "btn danger", type: "button", text: t("groups.resetAll", "全部恢复默认"),
        onclick: function () {
          if (!window.confirm(t("groups.resetAllConfirm", "将清除所有群的单独配置，改为跟随默认模板，确定吗？"))) return;
          apiPost("group/reset", {}).then(function () {
            state.groups = [];
            toast(t("groups.resetDone", "已全部恢复默认"), "ok");
            renderMain();
          }).catch(function (e) { toast(e.message, "err"); });
        }
      }));
    });
  }

  function showGroupDetail(host, gid) {
    clear(host).appendChild(el("div", { class: "loading", text: t("common.loading", "加载中…") }));
    return apiGet("group", { group_id: gid }).then(function (data) {
      clear(host);
      var info = state.groupIndex[gid] || {};
      var form = buildFieldForm(state.fields, data.values || {}, data.overrides || {});
      var head = el("div", { class: "toolbar" }, [
        el("strong", { text: plain(info.group_name, gid) }),
        el("span", { class: "tag mono", text: gid }),
        el("span", { class: "tag " + (data.follows_default ? "" : "warn"), text: data.follows_default ? t("groups.followDefault", "跟随默认") : t("groups.overrideCount", "覆写 ") + Object.keys(data.overrides || {}).length + t("groups.overrideItems", " 项") })
      ]);

      var saveBtn = el("button", { class: "btn primary", type: "button", text: t("common.save", "保存改动") });
      saveBtn.addEventListener("click", function () {
        var changes = form.collect();
        if (!Object.keys(changes).length) { toast(t("common.noChange", "没有需要保存的改动")); return; }
        saveBtn.disabled = true;
        apiPost("group/save", { group_id: gid, changes: changes }).then(function () {
          toast(t("common.saved", "已保存"), "ok");
          state.groups = [];
          loadGroups(false).then(function () { showGroupDetail(host, gid); });
        }).catch(function (e) { toast(e.message, "err"); }).then(function () { saveBtn.disabled = false; });
      });

      var resetBtn = el("button", { class: "btn danger", type: "button", text: t("groups.reset", "恢复为默认"), onclick: function () {
        if (!window.confirm(t("groups.resetConfirm", "清除本群的单独配置并跟随默认模板？"))) return;
        apiPost("group/reset", { group_id: gid }).then(function () {
          state.groups = [];
          toast(t("groups.resetDone2", "已恢复默认"), "ok");
          loadGroups(false).then(function () { showGroupDetail(host, gid); });
        }).catch(function (e) { toast(e.message, "err"); });
      } });

      host.appendChild(el("div", { class: "card" }, [head, form.node, el("div", { class: "toolbar", style: "margin-top:14px;margin-bottom:0" }, [saveBtn, resetBtn])]));

      var textArea = el("textarea", { value: data.text || "", style: "min-height:180px" });
      host.appendChild(section(
        t("groups.textTitle", "文本方式批量编辑"),
        el("div", {}, [
          textArea,
          el("div", { class: "toolbar", style: "margin-top:10px;margin-bottom:0" }, [
            el("button", { class: "btn", type: "button", text: t("groups.import", "按文本导入"), onclick: function () {
              apiPost("group/import", { group_id: gid, text: textArea.value }).then(function (res) {
                toast(t("groups.imported", "已导入 ") + (res.updated || []).length + t("groups.importedItems", " 项"), "ok");
                if ((res.unknown || []).length) toast(t("groups.unknownLines", "无法识别：") + res.unknown.join(" / "), "err");
                state.groups = [];
                loadGroups(false).then(function () { showGroupDetail(host, gid); });
              }).catch(function (e) { toast(e.message, "err"); });
            } })
          ])
        ]),
        t("groups.textNote", "格式与群内「群管配置」指令一致，每行「项目名: 值」，适合从别的群复制配置。")
      ));
    });
  }

  /* ------------------------------------------------------------ 默认模板 */

  function renderDefaults(body, actions) {
    return apiGet("defaults").then(function (data) {
      state.fields = data.fields || state.fields;
      clear(body);
      var form = buildFieldForm(state.fields, data.values || {}, null);
      var saveBtn = el("button", { class: "btn primary", type: "button", text: t("common.save", "保存改动") });
      saveBtn.addEventListener("click", function () {
        var changes = form.collect();
        if (!Object.keys(changes).length) { toast(t("common.noChange", "没有需要保存的改动")); return; }
        saveBtn.disabled = true;
        apiPost("defaults/save", { changes: changes }).then(function () {
          toast(t("common.saved", "已保存"), "ok");
          state.groups = [];
          renderMain();
        }).catch(function (e) { toast(e.message, "err"); }).then(function () { saveBtn.disabled = false; });
      });
      body.appendChild(section(
        t("defaults.title", "默认配置模板"),
        el("div", {}, [form.node, el("div", { class: "toolbar", style: "margin-top:14px;margin-bottom:0" }, saveBtn)]),
        t("defaults.note", "这里的值适用于所有还没有单独配置的群；已单独覆写的群不受影响。")
      ));
      actions.appendChild(el("button", { class: "btn", type: "button", text: t("common.refresh", "刷新"), onclick: renderMain }));
    });
  }

  /* ------------------------------------------------------------ 权限矩阵 */

  function renderPerms(body, actions) {
    return apiGet("perms").then(function (data) {
      clear(body);
      var selects = {};
      var rows = (data.rows || []).map(function (row) {
        var select = el("select", {}, (data.options || []).map(function (opt) {
          return el("option", { value: opt, text: opt, selected: opt === row.value });
        }));
        selects[row.key] = { node: select, initial: row.value };
        return [
          el("div", {}, [el("div", { text: row.label }), el("div", { class: "mono", text: row.key })]),
          select,
          el("span", { class: "tag", text: row.default })
        ];
      });
      var saveBtn = el("button", { class: "btn primary", type: "button", text: t("common.save", "保存改动") });
      saveBtn.addEventListener("click", function () {
        var changes = {};
        Object.keys(selects).forEach(function (key) {
          if (selects[key].node.value !== selects[key].initial) changes[key] = selects[key].node.value;
        });
        if (!Object.keys(changes).length) { toast(t("common.noChange", "没有需要保存的改动")); return; }
        saveBtn.disabled = true;
        apiPost("perms/save", { changes: changes }).then(function () {
          toast(t("common.saved", "已保存"), "ok");
          renderMain();
        }).catch(function (e) { toast(e.message, "err"); }).then(function () { saveBtn.disabled = false; });
      });
      body.appendChild(section(
        t("perms.title", "指令最低权限"),
        el("div", {}, [
          rows.length
            ? table([t("perms.command", "指令 / 能力"), t("perms.level", "最低身份"), t("perms.default", "默认值")], rows)
            : emptyBox(),
          el("div", { class: "toolbar", style: "margin-top:14px;margin-bottom:0" }, saveBtn)
        ]),
        t("perms.note", "身份从低到高：成员 < 高等级成员 < 管理员 < 群主 < 超管。超管即 AstrBot 配置中的管理员账号。")
      ));
      actions.appendChild(el("button", { class: "btn", type: "button", text: t("common.refresh", "刷新"), onclick: renderMain }));
    });
  }

  /* ---------------------------------------------------------------- 违禁词 */

  function renderWords(body) {
    return loadGroups(false).then(function () {
      clear(body);
      var pickerHost = el("div", { class: "card" });
      var detailHost = el("div", {});
      body.appendChild(pickerHost);
      body.appendChild(detailHost);
      var picker = groupPicker(pickerHost, function (gid) {
        state.currentGroup = gid;
        picker.repaint();
        showWords(detailHost, gid);
      });
      if (state.currentGroup) showWords(detailHost, state.currentGroup);
      else detailHost.appendChild(section(null, emptyBox(t("groups.pick", "先在上方选择一个群"))));
    });
  }

  function chipList(items, emptyText) {
    if (!items || !items.length) return el("span", { class: "field-hint", text: emptyText });
    return el("div", { class: "chips" }, items.map(function (word) { return el("span", { class: "tag", text: word }); }));
  }

  function showWords(host, gid) {
    clear(host).appendChild(el("div", { class: "loading", text: t("common.loading", "加载中…") }));
    return apiGet("words", { group_id: gid }).then(function (data) {
      clear(host);
      host.appendChild(el("div", { class: "grid cols-4" }, [
        statCard(t("words.custom", "自定义禁词"), (data.custom || []).length),
        statCard(t("words.builtin", "内置词库"), data.builtin_enabled ? data.builtin_count : t("common.off", "关闭"), data.builtin_enabled ? t("words.builtinOn", "已启用") : t("words.builtinOff", "未启用")),
        statCard(t("words.mode", "匹配方式"), plain(data.match_mode)),
        statCard(t("words.action", "处理方式"), plain(data.action), t("words.banTime", "禁言 ") + (data.ban_time || 0) + "s")
      ]));
      host.appendChild(section(t("words.customTitle", "自定义禁词"), chipList(data.custom, t("words.customEmpty", "尚未添加，可在群内发送「设置禁词 词1 词2」")), t("words.exempt", "豁免身份：") + plain(data.exempt_level)));
      host.appendChild(section(t("words.whitelistTitle", "白名单（命中也放行）"), chipList(data.whitelist, t("words.whitelistEmpty", "空"))));
      host.appendChild(el("p", { class: "card-note", text: t("words.editHint", "禁词内容通过群内指令或「群配置」页编辑，此处为只读汇总，避免两边写冲突。") }));
    });
  }

  /* -------------------------------------------------------------- 操作日志 */

  function renderAudit(body, actions) {
    clear(body);
    var filters = el("div", { class: "toolbar" });
    var listHost = el("div", {});
    body.appendChild(el("div", { class: "card" }, [filters, listHost]));

    var groupInput = el("input", { type: "search", placeholder: t("audit.group", "群号（留空为全部）"), value: state.audit.group_id });
    var keywordInput = el("input", { type: "search", placeholder: t("audit.keyword", "关键词：操作者 / 目标 / 详情"), value: state.audit.keyword });
    var limitSelect = el("select", {}, [20, 50, 100, 200].map(function (n) {
      return el("option", { value: String(n), text: n + t("audit.rows", " 条/页"), selected: n === state.audit.limit });
    }));

    function load() {
      state.audit.group_id = groupInput.value.trim();
      state.audit.keyword = keywordInput.value.trim();
      state.audit.limit = parseInt(limitSelect.value, 10) || 50;
      clear(listHost).appendChild(el("div", { class: "loading", text: t("common.loading", "加载中…") }));
      return apiGet("audit", {
        group_id: state.audit.group_id,
        keyword: state.audit.keyword,
        limit: String(state.audit.limit),
        offset: String(state.audit.offset)
      }).then(function (data) {
        clear(listHost);
        var rows = (data.rows || []).map(function (row) {
          return [
            el("span", { class: "mono", text: plain(row.created_text) }),
            el("span", { class: "tag accent", text: plain(row.action_label, row.action) }),
            el("span", { text: plain(row.group_id) }),
            el("span", {}, [
              el("div", { text: plain(row.operator_name, row.operator_id) }),
              el("div", { class: "mono", text: plain(row.operator_id, "") })
            ]),
            el("span", { text: plain(row.target_id) }),
            el("span", { text: plain(row.detail, "") }),
            el("span", { class: "tag " + (row.success ? "ok" : "err"), text: row.success ? t("audit.ok", "成功") : t("audit.fail", "失败") })
          ];
        });
        if (!rows.length) { listHost.appendChild(emptyBox(t("audit.empty", "没有符合条件的记录"))); }
        else {
          listHost.appendChild(table([
            t("audit.time", "时间"), t("audit.action", "操作"), t("common.group", "群号"),
            t("audit.operator", "操作者"), t("audit.target", "目标"), t("audit.detail", "详情"), t("common.status", "状态")
          ], rows));
        }
        var page1 = Math.floor(state.audit.offset / state.audit.limit) + 1;
        listHost.appendChild(el("div", { class: "toolbar", style: "margin-top:12px;margin-bottom:0" }, [
          el("button", {
            class: "btn small", type: "button", text: t("audit.prev", "上一页"), disabled: state.audit.offset <= 0,
            onclick: function () { state.audit.offset = Math.max(0, state.audit.offset - state.audit.limit); load(); }
          }),
          el("span", { class: "field-hint", text: t("audit.pageInfo", "第 ") + page1 + t("audit.pageOf", " 页 · 共 ") + (data.total || 0) + t("audit.records", " 条") }),
          el("button", {
            class: "btn small", type: "button", text: t("audit.next", "下一页"),
            disabled: (state.audit.offset + state.audit.limit) >= (data.total || 0),
            onclick: function () { state.audit.offset += state.audit.limit; load(); }
          })
        ]));
      });
    }

    filters.appendChild(el("div", { class: "grow" }, groupInput));
    filters.appendChild(el("div", { class: "grow" }, keywordInput));
    filters.appendChild(limitSelect);
    filters.appendChild(el("button", { class: "btn primary small", type: "button", text: t("common.query", "查询"), onclick: function () { state.audit.offset = 0; load().catch(function (e) { toast(e.message, "err"); }); } }));
    if (actions) {
      actions.appendChild(el("button", { class: "btn", type: "button", text: t("common.refresh", "刷新"), onclick: function () { load().catch(function (e) { toast(e.message, "err"); }); } }));
    }
    return load();
  }

  /* ------------------------------------------------------------ 待审进群 */

  function renderJoins(body, actions) {
    return apiGet("join/pending").then(function (items) {
      clear(body);
      var rows = (items || []).map(function (item) {
        return [
          el("span", { class: "tag accent", text: "#" + plain(item.seq) }),
          el("span", { text: plain(item.group_id) }),
          el("span", {}, [
            el("div", { text: plain(item.nickname, item.user_id) }),
            el("div", { class: "mono", text: plain(item.user_id) })
          ]),
          el("span", { text: plain(item.comment, "") }),
          el("span", { class: "mono", text: plain(item.created_text) })
        ];
      });
      body.appendChild(section(
        t("joins.title", "等待处理的入群申请"),
        rows.length
          ? table([t("joins.seq", "序号"), t("common.group", "群号"), t("joins.user", "申请人"), t("joins.comment", "验证信息"), t("joins.time", "申请时间")], rows)
          : emptyBox(t("joins.empty", "当前没有待处理的申请")),
        t("joins.note", "在对应群内发送「批准 序号」或「驳回 序号」即可处理；此处只做展示，避免网页与群内操作互相覆盖。")
      ));
      actions.appendChild(el("button", { class: "btn", type: "button", text: t("common.refresh", "刷新"), onclick: renderMain }));
    });
  }

  /* ---------------------------------------------------------------- 相册 */

  function renderAlbum(body) {
    return loadGroups(false).then(function () {
      clear(body);
      var pickerHost = el("div", { class: "card" });
      var detailHost = el("div", {});
      body.appendChild(pickerHost);
      body.appendChild(detailHost);
      var picker = groupPicker(pickerHost, function (gid) {
        state.currentGroup = gid;
        picker.repaint();
        showAlbum(detailHost, gid);
      });
      if (state.currentGroup) showAlbum(detailHost, state.currentGroup);
      else detailHost.appendChild(section(null, emptyBox(t("groups.pick", "先在上方选择一个群"))));
    });
  }

  function showAlbum(host, gid) {
    clear(host).appendChild(el("div", { class: "loading", text: t("common.loading", "加载中…") }));
    return apiGet("album", { group_id: gid }).then(function (data) {
      clear(host);
      host.appendChild(el("div", { class: "grid cols-4" }, [
        statCard(t("album.count", "相册数量"), (data.albums || []).length),
        statCard(t("album.backup", "本地备份"), data.backup_enabled ? t("common.on", "开启") : t("common.off", "关闭")),
        statCard(t("album.title", "显示头衔"), data.show_title ? t("common.on", "开启") : t("common.off", "关闭")),
        statCard(t("album.stitch", "拼接上限"), data.max_stitch_count || 0, t("album.stitchHint", "一次最多拼接的图片数"))
      ]));
      host.appendChild(section(
        t("album.listTitle", "群相册列表"),
        (data.albums || []).length
          ? table([t("album.name", "相册名"), "album_id"], (data.albums || []).map(function (item) {
              return [item.name, el("span", { class: "mono", text: item.album_id })];
            }))
          : emptyBox(t("album.listEmpty", "没有读取到相册，可能是协议端不支持或机器人不在群内")),
        t("album.listNote", "群内发送「上传群相册 相册名」并附带图片即可上传；相册功能需要 NapCat 4.8.100 以上或兼容实现。")
      ));
      host.appendChild(section(
        t("album.keywordTitle", "随机图关键词"),
        chipList(data.keywords, t("album.keywordEmpty", "本群未配置随机图关键词")),
        t("album.keywordNote", "在「全局设置 - 群相册」的随机相册中配置，群友发送关键词即可随机抽取相册内的图片。")
      ));
    });
  }

  /* -------------------------------------------------------------- 全局设置 */

  function renderSettings(body, actions) {
    return apiGet("settings").then(function (data) {
      clear(body);

      var scalarFields = (data.scalars || []).map(function (item) {
        return { field: item.key, label: item.label, type: item.type, hint: item.hint, options: item.options || [], default: item.value };
      });
      var scalarValues = {};
      (data.scalars || []).forEach(function (item) { scalarValues[item.key] = item.value; });
      var scalarForm = buildFieldForm(scalarFields, scalarValues, null);
      var scalarBtn = el("button", { class: "btn primary", type: "button", text: t("common.save", "保存改动") });
      scalarBtn.addEventListener("click", function () {
        var changes = scalarForm.collect();
        if (!Object.keys(changes).length) { toast(t("common.noChange", "没有需要保存的改动")); return; }
        scalarBtn.disabled = true;
        apiPost("settings/save", { section: "", changes: changes })
          .then(function () { toast(t("common.saved", "已保存"), "ok"); renderMain(); })
          .catch(function (e) { toast(e.message, "err"); })
          .then(function () { scalarBtn.disabled = false; });
      });
      body.appendChild(section(
        t("settings.basic", "基础项"),
        el("div", {}, [scalarForm.node, el("div", { class: "toolbar", style: "margin-top:14px;margin-bottom:0" }, scalarBtn)]),
        t("settings.basicNote", "影响插件整体行为的开关，保存后立即生效。")
      ));

      (data.sections || []).forEach(function (sec) {
        var fields = (sec.items || []).map(function (item) {
          return { field: item.key, label: item.label, type: item.type, hint: item.hint, options: item.options || [], default: item.value };
        });
        var values = {};
        (sec.items || []).forEach(function (item) { values[item.key] = item.value; });
        var form = buildFieldForm(fields, values, null);
        var btn = el("button", { class: "btn primary", type: "button", text: t("common.save", "保存改动") });
        btn.addEventListener("click", function () {
          var changes = form.collect();
          if (!Object.keys(changes).length) { toast(t("common.noChange", "没有需要保存的改动")); return; }
          btn.disabled = true;
          apiPost("settings/save", { section: sec.section, changes: changes })
            .then(function () { toast(t("common.saved", "已保存"), "ok"); renderMain(); })
            .catch(function (e) { toast(e.message, "err"); })
            .then(function () { btn.disabled = false; });
        });
        body.appendChild(section(sec.title, el("div", {}, [form.node, el("div", { class: "toolbar", style: "margin-top:14px;margin-bottom:0" }, btn)])));
      });

      actions.appendChild(el("button", { class: "btn", type: "button", text: t("common.refresh", "刷新"), onclick: renderMain }));
    });
  }

  /* ---------------------------------------------------------------- 启动 */

  function paintBrand() {
    var title = document.getElementById("brand-title");
    var version = document.getElementById("brand-version");
    if (title) title.textContent = state.displayName;
    if (version) version.textContent = state.version ? state.version : t("common.ready", "已连接");
  }

  function initTheme() {
    var stored = null;
    try { stored = window.localStorage.getItem("qun-steward-theme"); } catch (err) { stored = null; }
    var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    setTheme(stored || (prefersDark ? "dark" : "light"));
    var btn = document.getElementById("btn-theme");
    if (btn) {
      btn.textContent = t("common.theme", "切换深浅色");
      btn.addEventListener("click", function () {
        setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
      });
    }
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { window.localStorage.setItem("qun-steward-theme", theme); } catch (err) { /* 忽略隐私模式报错 */ }
  }

  function boot() {
    initTheme();
    var reload = document.getElementById("btn-reload");
    if (reload) {
      reload.textContent = t("common.reloadAll", "重新加载数据");
      reload.addEventListener("click", function () {
        state.groups = [];
        state.fields = [];
        renderMain();
      });
    }
    var hash = String(window.location.hash || "").replace("#", "");
    if (hash) state.view = hash;
    window.addEventListener("hashchange", function () {
      var next = String(window.location.hash || "").replace("#", "") || "overview";
      if (next !== state.view) navigate(next);
    });
    renderNav();
    paintBrand();
    renderMain();
    apiGet("ping").then(function (info) {
      if (info && info.version) { state.version = info.version; paintBrand(); }
    }).catch(function () { /* ping 失败时由主视图报错 */ });
  }

  function start() {
    if (page && typeof page.ready === "function") {
      Promise.resolve(page.ready()).then(function (info) {
        if (info && info.displayName) state.displayName = info.displayName;
        boot();
      }).catch(function () { boot(); });
    } else {
      boot();
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
