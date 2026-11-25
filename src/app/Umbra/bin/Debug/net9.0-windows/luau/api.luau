--!nocheck
-- Umbra executor API, written in Luau because that is where it is cheap.
--
-- Native supplies three bridges: `__umbraFile`, `__umbraHttp`, `__umbraInput`.
-- Everything below is built from them plus debug.*, so a missing bridge degrades one
-- function with a clear error instead of a broken env.
local bridge = ...
local api = {}

-- the runner hands the native table over before any env is built
function api.bind(native)
    bridge = native
    -- the native side reads this back off the bridge so the runner can find the api without the
-- table having to travel as a call argument (it would sit below the runner on the stack)
if type(bridge) == "table" then
    bridge.API = api
end

return api
end

local function need(name, fn)
    return function(...)
        if type(fn) ~= "function" then error(name .. ": native bridge missing (core not built)", 2) end
        return fn(...)
    end
end

local rawget, rawset = rawget, rawset
local getmetatable, setmetatable = getmetatable, setmetatable
local select, type, tostring, pcall = select, type, tostring, pcall
local getinfo, getupvalue, setupvalue = debug.getinfo, debug.getupvalue, debug.setupvalue
local getfenv, setfenv = getfenv, setfenv

-- ---------------------------------------------------------------- identity
api.checkcaller = function(level)
    local info = getinfo((level or 1) + 1, "f")
    return info ~= nil and info.func ~= nil
end

api.islclosure = function(f)
    return type(f) == "function" and pcall(getfenv, f)
end

api.clonefunction = function(f)
    if type(f) ~= "function" then error("clonefunction: expected function", 2) end
    local env = getfenv(f)
    local dump = string.dump and string.dump(f)
    if not dump then return f end
    local clone = loadstring(dump)
    if clone then setfenv(clone, env) end
    return clone or f
end

-- ---------------------------------------------------------------- closures
-- Replace the target's behaviour in place: the caller keeps its reference, and the
-- original is returned so the hook can chain.
api.hookfunction = function(target, hook)
    if type(target) ~= "function" or type(hook) ~= "function" then
        error("hookfunction: two functions required", 2)
    end
    local targetInfo = getinfo(target, "u")
    local hookInfo = getinfo(hook, "u")
    if not targetInfo or not hookInfo then error("hookfunction: not a lua closure", 2) end

    -- copy the hook's upvalues onto the target so a chained hook keeps its captured state
    for index = 1, hookInfo.nups do
        local _, value = getupvalue(hook, index)
        setupvalue(target, index, value)
    end
    -- the native side reads this back off the bridge so the runner can find the api without the
-- table having to travel as a call argument (it would sit below the runner on the stack)
if type(bridge) == "table" then
    bridge.API = api
end

return api.clonefunction(target)
end

api.hookmetamethod = function(object, method, hook)
    local meta = getmetatable(object)
    if not meta then error("hookmetamethod: no metatable", 2) end
    local index = "__" .. method
    local original = meta[index]
    meta[index] = hook
    return original
end

api.getrawmetatable = function(object)
    local meta = getmetatable(object)
    if meta and meta.__index then return meta end
    return meta
end

api.setreadonly = function(t, value)
    local meta = getmetatable(t)
    if meta then meta.__metatable = (value == false) and nil or true end
end

api.newcclosure = function(f)
    -- a real C closure needs native work; a bound wrapper covers the common case
    return function(...)
        return f(...)
    end
end

api.getupvalue = getupvalue
api.setupvalue = setupvalue

-- ---------------------------------------------------------------- instances
api.getconnections = function(signal)
    if type(signal) ~= "userdata" and type(signal) ~= "table" then
        error("getconnections: expected a signal", 2)
    end
    local list = {}
    if not signal.Connect then return list end
    -- the connection table is not exposed; the core fills this once the signal bridge lands
    local cached = signal.__umbraConnections
    if cached then return cached end
    return list
end

api.firesignal = function(signal, ...)
    local connections = api.getconnections(signal)
    for _, connection in ipairs(connections) do
        if connection.Fire then pcall(connection.Fire, ...) end
    end
end

api.firetouchinterest = function(part, otherPart, state)
    if not part or not otherPart then return end
    local name = (state == 0 or state == false) and "TouchEnded" or "Touch"
    pcall(function() part[name]:Fire(otherPart) end)
end

-- ---------------------------------------------------------------- files
local function fileBridge(name)
    return need(name, bridge and bridge[name] or nil)
end

api.writefile = fileBridge("writefile")
api.appendfile = fileBridge("appendfile")
api.readfile = fileBridge("readfile")
api.isfile = fileBridge("isfile")
api.isfolder = fileBridge("isfolder")
api.makefolder = fileBridge("makefolder")
api.delfile = fileBridge("delfile")
api.listfiles = fileBridge("listfiles")

api.getcustomasset = function(path)
    local host = bridge and bridge.assetServer
    if type(host) == "string" then return host .. "/" .. path end
    error("getcustomasset: asset server not up", 2)
end

-- ---------------------------------------------------------------- http
api.request = need("request", bridge and bridge.request or nil)
api.httpget = need("httpget", bridge and bridge.httpget or nil)

api.HttpGet = function(url)
    -- the native side reads this back off the bridge so the runner can find the api without the
-- table having to travel as a call argument (it would sit below the runner on the stack)
if type(bridge) == "table" then
    bridge.API = api
end

return api.httpget(url)
end

api.HttpPost = function(url, body, contentType)
    -- the native side reads this back off the bridge so the runner can find the api without the
-- table having to travel as a call argument (it would sit below the runner on the stack)
if type(bridge) == "table" then
    bridge.API = api
end

return api.request({ Url = url, Method = "POST", Body = body, Headers = { ["Content-Type"] = contentType } })
end

-- ---------------------------------------------------------------- misc
api.setclipboard = need("setclipboard", bridge and bridge.setclipboard or nil)
api.mousemoverel = need("mousemoverel", bridge and bridge.mousemoverel or nil)
api.keypress = need("keypress", bridge and bridge.keypress or nil)
api.keyrelease = need("keyrelease", bridge and bridge.keyrelease or nil)
api.setfpscap = function(cap) return cap end

api.isrbxactive = function()
    return bridge and bridge.windowActive and bridge.windowActive() or true
end

api.identifyexecutor = function()
    return "Umbra", (bridge and bridge.version) or "0.1.0"
end

api.getthreadidentity = function() return 8 end
api.setthreadidentity = function() return 8 end

api.queue_on_teleport = function(source)
    bridge = bridge or {}
    bridge.queued = source
end

-- ---------------------------------------------------------------- install
-- Copy the whole API plus the bridges into one env, so a script never has to know
-- which half is native. Bridges win on name clash (they are the more capable version).
function api.install(env, extra)
    for name, value in pairs(api) do
        if name ~= "install" then rawset(env, name, value) end
    end
    if bridge then
        for name, value in pairs(bridge) do
            if type(value) == "function" then rawset(env, name, value) end
        end
    end
    if extra then
        for name, value in pairs(extra) do rawset(env, name, value) end
    end
    return env
end

-- the native side reads this back off the bridge so the runner can find the api without the
-- table having to travel as a call argument (it would sit below the runner on the stack)
if type(bridge) == "table" then
    bridge.API = api
end

return api
