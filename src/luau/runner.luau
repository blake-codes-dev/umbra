--!nocheck
-- Lyphen runner. The only chunk native code ever calls: `task.spawn(runnerChunk)`.
-- Everything after that is Luau, inside the client's own scheduler, so no yield ever
-- crosses a native frame. See docs/protocol.md for the bus layout this drains.

local RunService = game:GetService("RunService")

local BRIDGE, BUS = ...
-- the compiler is captured once: inside a script env `loadstring` is the env's own field, so
-- looking it up dynamically there would recurse into itself
local rawLoad = BRIDGE.loadstring or loadstring
-- api.luau travels as source inside the bridge and is compiled here, so the only native call
-- is the one that starts this chunk
local API
do
    if type(BRIDGE.loadstring) ~= "function" then
        error("lyphen: the core has no compiler (loadstring bridge missing)")
    end
    local chunk, loadError = BRIDGE.loadstring(BRIDGE.apiSource)
    if not chunk then error("lyphen: api did not compile: " .. tostring(loadError)) end
    API = chunk(BRIDGE)
end
local OP_EXEC = 1
local OP_CANCEL = 2
local OP_PROBE = 3
local OP_SHUTDOWN = 4

local OUT_PRINT = 1
local OUT_WARN = 2
local OUT_ERROR = 3
local OUT_INFO = 4
local OUT_DONE = 5

-- the single shared table every script sees through getgenv(), plus the script cache
local genv = {}
local running = {}
local jobs = {}
local nextJob = 0

local function write(kind, id, text)
    if type(text) ~= "string" then text = tostring(text) end
    if #text > 480 then text = text:sub(1, 477) .. "..." end
    BUS.push(kind, id, text)
end

--[[ one environment per script: globals do not leak between runs, but genv does ]]
local function makeEnv(id)
    local env = setmetatable({}, { __index = _G })
    -- bridges first: they are the wider surface, and the few functions this runner must own
    -- (print, warn, loadstring, getgenv) are assigned after so they cannot be overwritten
    if API then API.install(env) end
    env.script = nil
    env.getgenv = function() return genv end
    env.getrenv = function() return env end
    env.shared = genv
    env.identifyexecutor = function() return "Lyphen", "0.1.0" end
    env.getthreadidentity = function() return 8 end
    local identity = 8

    env.task = setmetatable({}, { __index = task })
    env.task.wait = function(t) return task.wait(t) end
    env.print = function(...)
        local parts = {}
        for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
        write(OUT_PRINT, id, table.concat(parts, " "))
    end
    env.warn = function(...)
        local parts = {}
        for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
        write(OUT_WARN, id, table.concat(parts, " "))
    end

    -- loadstring compiles against this env, never the global one
    env.loadstring = function(source, chunkName)
        local chunk, err = rawLoad(source, chunkName or ("@Lyphen: " .. id))
        if not chunk then return nil, err end
        setfenv(chunk, env)
        return chunk
    end
    env.load = env.loadstring

    env.require = function(target)
        local ok, result = pcall(require, target)
        if not ok then error(result, 2) end
        return result
    end
    env.__identity = identity
    return env
end

local function cancel(id)
    local thread = running[id]
    if thread then
        pcall(task.cancel, thread)
        running[id] = nil
        write(OUT_INFO, id, "cancelled")
    end
end

--[[ run one command; never yields the runner itself ]]
local function start(id, source)
    local env = makeEnv(id)
    local chunk, compileError = env.loadstring(source)
    if not chunk then
        write(OUT_ERROR, id, compileError or "compile failed")
        write(OUT_DONE, id, "0")
        return
    end
    running[id] = task.spawn(function()
        local ok, result = xpcall(chunk, function(message)
            return tostring(message) .. "\n" .. debug.traceback(nil, 2)
        end)
        running[id] = nil
        if not ok then write(OUT_ERROR, id, result) end
        write(OUT_DONE, id, ok and "1" or "0")
    end)
end

local function step()
    local batch = BUS.take(8)
    if not batch then return end
    for _, command in ipairs(batch) do
        if command.op == OP_EXEC then
            start(command.id, command.payload)
        elseif command.op == OP_CANCEL then
            cancel(command.id)
        elseif command.op == OP_PROBE then
            write(OUT_INFO, command.id, "alive, " .. tostring(nextJob) .. " jobs")
        elseif command.op == OP_SHUTDOWN then
            for id in pairs(running) do cancel(id) end
            BUS.stop()
        end
    end
end

-- heartbeat, not RenderStepped: it keeps running while the client is unfocused, and a
-- script that yields cannot stall it because every script gets its own thread
RunService.Heartbeat:Connect(function()
    -- the core has no thread: this is where its pipe is serviced
    if type(BRIDGE.pump) == "function" then pcall(BRIDGE.pump) end
    local ok, err = pcall(step)
    if not ok then write(OUT_ERROR, 0, "runner: " .. tostring(err)) end
end)

write(OUT_INFO, 0, "runner up")

genv.__lyphenRunner = {
    version = "0.1.0",
    exec = function(source)
        nextJob += 1
        start(nextJob, source)
        return nextJob
    end,
    cancel = cancel,
}
