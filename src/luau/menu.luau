--!nocheck
-- name: mod menu
-- desc: the panel itself, tabs and shipped scripts, run this one
-- Umbra menu. Single file, no dependencies, no loadstring.
-- Toggle with RightShift. Drag with the title bar. Config is per-place.

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")
local Lighting = game:GetService("Lighting")
local Workspace = game:GetService("Workspace")
local StarterGui = game:GetService("StarterGui")
local TweenService = game:GetService("TweenService")

local LocalPlayer = Players.LocalPlayer
local Camera = Workspace.CurrentCamera

local ACCENT = Color3.fromRGB(124, 92, 255)
local BG = Color3.fromRGB(11, 11, 16)
local PANEL = Color3.fromRGB(18, 18, 28)
local ROW = Color3.fromRGB(24, 24, 38)
local LINE = Color3.fromRGB(38, 38, 58)
local TEXT = Color3.fromRGB(232, 232, 242)
local DIM = Color3.fromRGB(150, 150, 175)
local GOOD = Color3.fromRGB(62, 213, 152)

local state = {
    speed = 16,
    jump = 50,
    fullbright = false,
    boxes = false,
    names = false,
    tracers = false,
    noClip = false,
    infJump = false,
    restored = {},
}

local connections = {}
local espObjects = {}

local function track(connection)
    table.insert(connections, connection)
    return connection
end

local function corner(parent, radius)
    local mod = Instance.new("UICorner")
    mod.CornerRadius = UDim.new(0, radius or 8)
    mod.Parent = parent
    return mod
end

local function stroke(parent, color, thickness)
    local mod = Instance.new("UIStroke")
    mod.Color = color or LINE
    mod.Thickness = thickness or 1
    mod.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
    mod.Parent = parent
    return mod
end

local function pad(parent, amount)
    local mod = Instance.new("UIPadding")
    mod.PaddingTop = UDim.new(0, amount)
    mod.PaddingBottom = UDim.new(0, amount)
    mod.PaddingLeft = UDim.new(0, amount)
    mod.PaddingRight = UDim.new(0, amount)
    mod.Parent = parent
    return mod
end

local function label(parent, text, size, color, bold)
    local item = Instance.new("TextLabel")
    item.BackgroundTransparency = 1
    item.Text = text
    item.TextSize = size or 13
    item.TextColor3 = color or TEXT
    item.Font = bold and Enum.Font.GothamBold or Enum.Font.Gotham
    item.TextXAlignment = Enum.TextXAlignment.Left
    item.Parent = parent
    return item
end

local function button(parent, text, height)
    local item = Instance.new("TextButton")
    item.BackgroundColor3 = ROW
    item.Text = text
    item.TextSize = 12
    item.TextColor3 = TEXT
    item.Font = Enum.Font.GothamMedium
    item.Size = UDim2.new(1, 0, 0, height or 28)
    item.AutoButtonColor = true
    item.Parent = parent
    corner(item, 6)
    stroke(item)
    return item
end

-- motion: one easing curve everywhere, 0.16 s, so nothing snaps
local EASE = TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out)

local function tween(object, goal)
    TweenService:Create(object, EASE, goal):Play()
end

local function hover(item, idleColor, hotColor)
    track(item.MouseEnter:Connect(function() tween(item, { BackgroundColor3 = hotColor }) end))
    track(item.MouseLeave:Connect(function() tween(item, { BackgroundColor3 = idleColor }) end))
end

local toasts = nil

local function toast(text, good)
    if not toasts then
        toasts = Instance.new("Frame")
        toasts.Size = UDim2.new(0, 240, 0, 300)
        toasts.Position = UDim2.new(1, -260, 1, -320)
        toasts.BackgroundTransparency = 1
        toasts.Parent = root.Parent
        local layout = Instance.new("UIListLayout")
        layout.VerticalAlignment = Enum.VerticalAlignment.Bottom
        layout.Padding = UDim.new(0, 6)
        layout.Parent = toasts
    end
    local card = Instance.new("Frame")
    card.Size = UDim2.new(1, 0, 0, 30)
    card.BackgroundColor3 = good and Color3.fromRGB(24, 40, 32) or Color3.fromRGB(34, 24, 34)
    card.BorderSizePixel = 0
    card.Parent = toasts
    corner(card, 7)
    stroke(card, good and GOOD or Color3.fromRGB(255, 93, 108))
    local text_ = label(card, text, 11, good and GOOD or Color3.fromRGB(255, 120, 130))
    text_.Position = UDim2.new(0, 10, 0, 7)
    text_.Size = UDim2.new(1, -20, 0, 16)
    text_.TextTruncate = Enum.TextTruncate.AtEnd
    task.delay(2.4, function()
        tween(card, { BackgroundTransparency = 1 })
        task.delay(0.2, function() card:Destroy() end)
    end)
end

-- window ---------------------------------------------------------------

local gui = Instance.new("ScreenGui")
gui.Name = "UmbraMenu"
gui.ResetOnSpawn = false
gui.IgnoreGuiInset = true
gui.ZIndexBehavior = Enum.ZIndexBehavior.Sibling
gui.DisplayOrder = 999
gui.Parent = LocalPlayer:WaitForChild("PlayerGui")

local root = Instance.new("Frame")
root.Size = UDim2.new(0, 520, 0, 380)
root.Position = UDim2.new(0, 120, 0, 120)
root.BackgroundColor3 = BG
root.BorderSizePixel = 0
root.Visible = false
root.Parent = gui
corner(root, 12)
stroke(root)

local scale = Instance.new("UIScale")
scale.Scale = 1
scale.Parent = root

local shown = false

local function show()
    if shown then return end
    shown = true
    root.Visible = true
    root.Position = UDim2.new(0, 120, 0, 132)
    scale.Scale = 0.96
    tween(root, { Position = UDim2.new(0, 120, 0, 120) })
    tween(scale, { Scale = 1 })
end

local function hide()
    if not shown then return end
    shown = false
    tween(root, { Position = UDim2.new(0, 120, 0, 132) })
    local step = TweenService:Create(scale, EASE, { Scale = 0.96 })
    step:Play()
    step.Completed:Once(function()
        if not shown then root.Visible = false end
    end)
end

local titleBar = Instance.new("Frame")
titleBar.Size = UDim2.new(1, 0, 0, 34)
titleBar.BackgroundColor3 = PANEL
titleBar.BorderSizePixel = 0
titleBar.Parent = root
corner(titleBar, 10)

local dot = Instance.new("Frame")
dot.Size = UDim2.new(0, 8, 0, 8)
dot.Position = UDim2.new(0, 12, 0.5, -4)
dot.BackgroundColor3 = ACCENT
dot.BorderSizePixel = 0
dot.Parent = titleBar
corner(dot, 4)

local brand = label(titleBar, "UMBRA", 12, TEXT, true)
brand.Position = UDim2.new(0, 26, 0, 9)
brand.Size = UDim2.new(0, 120, 0, 16)

local statusLabel = label(titleBar, "idle", 11, DIM)
statusLabel.Position = UDim2.new(1, -128, 0, 10)
statusLabel.Size = UDim2.new(0, 78, 0, 14)
statusLabel.TextXAlignment = Enum.TextXAlignment.Right

local closeButton = Instance.new("TextButton")
closeButton.Size = UDim2.new(0, 24, 0, 24)
closeButton.Position = UDim2.new(1, -32, 0, 5)
closeButton.BackgroundColor3 = ROW
closeButton.Text = "x"
closeButton.TextSize = 12
closeButton.TextColor3 = DIM
closeButton.Font = Enum.Font.GothamBold
closeButton.Parent = titleBar
corner(closeButton, 6)
hover(closeButton, ROW, Color3.fromRGB(40, 30, 46))
track(closeButton.MouseButton1Click.Connect(function()
    hide()
end))

-- tabs ----------------------------------------------------------------

local tabBar = Instance.new("Frame")
tabBar.Size = UDim2.new(0, 108, 1, -34)
tabBar.Position = UDim2.new(0, 0, 0, 34)
tabBar.BackgroundColor3 = PANEL
tabBar.BorderSizePixel = 0
tabBar.Parent = root
pad(tabBar, 8)

local tabLayout = Instance.new("UIListLayout")
tabLayout.Padding = UDim.new(0, 4)
tabLayout.SortOrder = Enum.SortOrder.LayoutOrder
tabLayout.Parent = tabBar

local content = Instance.new("Frame")
content.Size = UDim2.new(1, -108, 1, -34)
content.Position = UDim2.new(0, 108, 0, 34)
content.BackgroundTransparency = 1
content.Parent = root
pad(content, 12)

local pages = {}
local buttons = {}

local function page(name)
    local pageFrame = Instance.new("Frame")
    pageFrame.Size = UDim2.new(1, 0, 1, 0)
    pageFrame.BackgroundTransparency = 1
    pageFrame.Visible = false
    pageFrame.Parent = content

    local layout = Instance.new("UIListLayout")
    layout.Padding = UDim.new(0, 8)
    layout.SortOrder = Enum.SortOrder.LayoutOrder
    layout.Parent = pageFrame

    local tab = button(tabBar, name, 26)
    tab.TextXAlignment = Enum.TextXAlignment.Left
    pad(tab, 2)

    buttons[name] = tab
    pages[name] = pageFrame

    track(tab.MouseButton1Click.Connect(function()
        for other, frame in pages do
            frame.Visible = other == name
            buttons[other].BackgroundColor3 = other == name and ACCENT or ROW
            buttons[other].TextColor3 = other == name and TEXT or DIM
        end
        statusLabel.Text = string.lower(name)
    end))
    return pageFrame
end

local function toggle(parent, text, initial, callback)
    local row = Instance.new("Frame")
    row.Size = UDim2.new(1, 0, 0, 30)
    row.BackgroundColor3 = ROW
    row.BorderSizePixel = 0
    row.Parent = parent
    corner(row, 6)

    label(row, text, 12, TEXT).Position = UDim2.new(0, 10, 0, 7)

    local pill = Instance.new("TextButton")
    pill.Size = UDim2.new(0, 34, 0, 16)
    pill.Position = UDim2.new(1, -44, 0.5, -8)
    pill.BackgroundColor3 = initial and ACCENT or LINE
    pill.Text = ""
    pill.Parent = row
    corner(pill, 8)

    local knob = Instance.new("Frame")
    knob.Size = UDim2.new(0, 12, 0, 12)
    knob.Position = UDim2.new(0, initial and 20 or 2, 0.5, -6)
    knob.BackgroundColor3 = TEXT
    knob.BorderSizePixel = 0
    knob.Parent = pill
    corner(knob, 6)

    local on = initial
    track(pill.MouseButton1Click.Connect(function()
        on = not on
        pill.BackgroundColor3 = on and ACCENT or LINE
        knob.Position = UDim2.new(0, on and 20 or 2, 0.5, -6)
        callback(on)
    end))
    return row
end

local function slider(parent, text, min, max, initial, callback)
    local row = Instance.new("Frame")
    row.Size = UDim2.new(1, 0, 0, 40)
    row.BackgroundColor3 = ROW
    row.BorderSizePixel = 0
    row.Parent = parent
    corner(row, 6)

    local caption = label(row, text .. "  " .. tostring(initial), 12, TEXT)
    caption.Position = UDim2.new(0, 10, 0, 4)

    local bar = Instance.new("Frame")
    bar.Size = UDim2.new(1, -20, 0, 4)
    bar.Position = UDim2.new(0, 10, 0, 26)
    bar.BackgroundColor3 = LINE
    bar.BorderSizePixel = 0
    bar.Parent = row
    corner(bar, 2)

    local fill = Instance.new("Frame")
    fill.Size = UDim2.new((initial - min) / (max - min), 0, 1, 0)
    fill.BackgroundColor3 = ACCENT
    fill.BorderSizePixel = 0
    fill.Parent = bar
    corner(fill, 2)

    local function apply(x)
        local alpha = math.clamp((x - bar.AbsolutePosition.X) / bar.AbsoluteSize.X, 0, 1)
        local value = math.floor(min + (max - min) * alpha + 0.5)
        fill.Size = UDim2.new(alpha, 0, 1, 0)
        caption.Text = text .. "  " .. tostring(value)
        callback(value)
    end

    track(bar.InputBegan:Connect(function(input)
        if input.UserInputType == Enum.UserInputType.MouseButton1 then apply(input.Position.X) end
    end))
    track(UserInputService.InputChanged:Connect(function(input)
        if input.UserInputType == Enum.UserInputType.MouseMovement
            and UserInputService:IsMouseButtonPressed(Enum.UserInputType.MouseButton1)
            and bar:IsHovered() then
            apply(input.Position.X)
        end
    end))
    return row
end

-- features ------------------------------------------------------------

local function character()
    local char = LocalPlayer.Character
    return char and char:FindFirstChildOfClass("Humanoid") and char or nil
end

local function applySpeed()
    local char = character()
    if char then char:FindFirstChildOfClass("Humanoid").WalkSpeed = state.speed end
end

local function applyJump()
    local char = character()
    if char then char:FindFirstChildOfClass("Humanoid").JumpPower = state.jump end
end

local function setFullbright(on)
    if on then
        state.restored.brightness = Lighting.Brightness
        Lighting.Brightness = 2
        Lighting.ClockTime = 14
        Lighting.FogEnd = 1e5
    else
        Lighting.Brightness = state.restored.brightness or 1
    end
end

local function drawBox(player)
    local box = Drawing.new("Square")
    box.Thickness = 1
    box.Color = ACCENT
    box.Filled = false
    espObjects[player] = { box = box, name = Drawing.new("Text") }
    local entry = espObjects[player]
    entry.name.Size = 14
    entry.name.Center = true
    entry.name.Outline = true
end

local function clearEsp()
    for _, entry in espObjects do
        entry.box:Remove()
        entry.name:Remove()
    end
    espObjects = {}
end

track(RunService.RenderStepped:Connect(function()
    if not (state.boxes or state.names or state.tracers) then return end
    for _, player in Players:GetPlayers() do
        if player == LocalPlayer then continue end
        local entry = espObjects[player]
        if not entry then
            drawBox(player)
            entry = espObjects[player]
        end
        local char = player.Character
        local head = char and char:FindFirstChild("Head")
        local humanoid = char and char:FindFirstChildOfClass("Humanoid")
        local visible = head ~= nil and humanoid ~= nil and humanoid.Health > 0
        entry.box.Visible = visible and state.boxes
        entry.name.Visible = visible and state.names
        if visible then
            local point, onScreen = Camera:WorldToViewportPoint(head.Position)
            if onScreen then
                local scale = 1400 / point.Z
                entry.box.Size = Vector2.new(scale * 0.6, scale * 1.4)
                entry.box.Position = Vector2.new(point.X - entry.box.Size.X / 2,
                                                 point.Y - entry.box.Size.Y / 2)
                entry.name.Text = player.Name .. "  " .. math.floor(humanoid.Health)
                entry.name.Position = Vector2.new(point.X, point.Y - 18)
            else
                entry.box.Visible = false
                entry.name.Visible = false
            end
        end
    end
end))

track(RunService.Stepped:Connect(function()
    if state.noClip then
        local char = character()
        if char then
            for _, part in char:GetDescendants() do
                if part:IsA("BasePart") then part.CanCollide = false end
            end
        end
    end
    if state.infJump then
        local char = character()
        if char then char:FindFirstChildOfClass("Humanoid"):ChangeState(Enum.HumanoidStateType.Jumping) end
    end
end))

track(LocalPlayer.CharacterAdded:Connect(function()
    task.wait(1)
    applySpeed()
    applyJump()
end))

-- pages ---------------------------------------------------------------

-- shipped scripts: name/desc/source/code so one click runs without loadstring
-- [[LIBRARY-BEGIN]] generated by tools/build_menu.py, do not hand-edit
local LIBRARY = {
    { name = "walk speed", desc = "sets your walkspeed to 100 and keeps it through respawns",
      source = [==[
local Players = game:GetService("Players")
local player = Players.LocalPlayer

local function set(char)
    local humanoid = char:WaitForChild("Humanoid", 5)
    if humanoid then humanoid.WalkSpeed = 100 end
end

if player.Character then set(player.Character) end
player.CharacterAdded:Connect(set)
]==],
      code = function()
        local Players = game:GetService("Players")
        local player = Players.LocalPlayer

        local function set(char)
            local humanoid = char:WaitForChild("Humanoid", 5)
            if humanoid then humanoid.WalkSpeed = 100 end
        end

        if player.Character then set(player.Character) end
        player.CharacterAdded:Connect(set)
      end },
    { name = "super jump", desc = "jumppower 200, survives respawn",
      source = [==[
local Players = game:GetService("Players")
local player = Players.LocalPlayer

local function set(char)
    local humanoid = char:WaitForChild("Humanoid", 5)
    if humanoid then humanoid.JumpPower = 200 end
end

if player.Character then set(player.Character) end
player.CharacterAdded:Connect(set)
]==],
      code = function()
        local Players = game:GetService("Players")
        local player = Players.LocalPlayer

        local function set(char)
            local humanoid = char:WaitForChild("Humanoid", 5)
            if humanoid then humanoid.JumpPower = 200 end
        end

        if player.Character then set(player.Character) end
        player.CharacterAdded:Connect(set)
      end },
    { name = "infinite jump", desc = "jump in mid air, unlimited. load again to turn off",
      source = [==[
local Players = game:GetService("Players")
local UserInputService = game:GetService("UserInputService")

if _G.__umbraInfJump then
    _G.__umbraInfJump:Disconnect()
    _G.__umbraInfJump = nil
else
    _G.__umbraInfJump = UserInputService.JumpRequest:Connect(function()
        local char = Players.LocalPlayer.Character
        local humanoid = char and char:FindFirstChildOfClass("Humanoid")
        if humanoid then humanoid:ChangeState(Enum.HumanoidStateType.Jumping) end
    end)
end
]==],
      code = function()
        local Players = game:GetService("Players")
        local UserInputService = game:GetService("UserInputService")

        if _G.__umbraInfJump then
            _G.__umbraInfJump:Disconnect()
            _G.__umbraInfJump = nil
        else
            _G.__umbraInfJump = UserInputService.JumpRequest:Connect(function()
                local char = Players.LocalPlayer.Character
                local humanoid = char and char:FindFirstChildOfClass("Humanoid")
                if humanoid then humanoid:ChangeState(Enum.HumanoidStateType.Jumping) end
            end)
        end
      end },
    { name = "noclip", desc = "walk through walls. load again to turn off",
      source = [==[
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

if _G.__umbraNoclip then
    _G.__umbraNoclip:Disconnect()
    _G.__umbraNoclip = nil
else
    _G.__umbraNoclip = RunService.Stepped:Connect(function()
        local char = Players.LocalPlayer.Character
        if not char then return end
        for _, part in ipairs(char:GetDescendants()) do
            if part:IsA("BasePart") then part.CanCollide = false end
        end
    end)
end
]==],
      code = function()
        local Players = game:GetService("Players")
        local RunService = game:GetService("RunService")

        if _G.__umbraNoclip then
            _G.__umbraNoclip:Disconnect()
            _G.__umbraNoclip = nil
        else
            _G.__umbraNoclip = RunService.Stepped:Connect(function()
                local char = Players.LocalPlayer.Character
                if not char then return end
                for _, part in ipairs(char:GetDescendants()) do
                    if part:IsA("BasePart") then part.CanCollide = false end
                end
            end)
        end
      end },
    { name = "full heal", desc = "refills your health on the spot",
      source = [==[
local char = game:GetService("Players").LocalPlayer.Character
local humanoid = char and char:FindFirstChildOfClass("Humanoid")
if humanoid then humanoid.Health = humanoid.MaxHealth end
]==],
      code = function()
        local char = game:GetService("Players").LocalPlayer.Character
        local humanoid = char and char:FindFirstChildOfClass("Humanoid")
        if humanoid then humanoid.Health = humanoid.MaxHealth end
      end },
    { name = "respawn", desc = "resets your character",
      source = [==[
local char = game:GetService("Players").LocalPlayer.Character
local humanoid = char and char:FindFirstChildOfClass("Humanoid")
if humanoid then humanoid.Health = 0 end
]==],
      code = function()
        local char = game:GetService("Players").LocalPlayer.Character
        local humanoid = char and char:FindFirstChildOfClass("Humanoid")
        if humanoid then humanoid.Health = 0 end
      end },
    { name = "anti afk", desc = "nudges the client so the 20 minute idle kick never fires",
      source = [==[
local VirtualUser = game:GetService("VirtualUser")
local player = game:GetService("Players").LocalPlayer

if _G.__umbraAfk then _G.__umbraAfk:Disconnect() end
_G.__umbraAfk = player.Idled:Connect(function()
    VirtualUser:CaptureController()
    VirtualUser:ClickButton2(Vector2.new())
end)
]==],
      code = function()
        local VirtualUser = game:GetService("VirtualUser")
        local player = game:GetService("Players").LocalPlayer

        if _G.__umbraAfk then _G.__umbraAfk:Disconnect() end
        _G.__umbraAfk = player.Idled:Connect(function()
            VirtualUser:CaptureController()
            VirtualUser:ClickButton2(Vector2.new())
        end)
      end },
    { name = "teleport to cursor", desc = "puts you where the mouse points, in front of the camera",
      source = [==[
local Players = game:GetService("Players")
local UserInputService = game:GetService("UserInputService")
local Workspace = game:GetService("Workspace")

local player = Players.LocalPlayer
local char = player.Character
local root = char and char:FindFirstChild("HumanoidRootPart")
local camera = Workspace.CurrentCamera
if not root or not camera then return end

local mouse = player:GetMouse()
local target = mouse.Hit
if not target then return end
local offset = (camera.CFrame.LookVector * 4)
root.CFrame = CFrame.new(target.Position + offset + Vector3.new(0, 3, 0))
]==],
      code = function()
        local Players = game:GetService("Players")
        local UserInputService = game:GetService("UserInputService")
        local Workspace = game:GetService("Workspace")

        local player = Players.LocalPlayer
        local char = player.Character
        local root = char and char:FindFirstChild("HumanoidRootPart")
        local camera = Workspace.CurrentCamera
        if not root or not camera then return end

        local mouse = player:GetMouse()
        local target = mouse.Hit
        if not target then return end
        local offset = (camera.CFrame.LookVector * 4)
        root.CFrame = CFrame.new(target.Position + offset + Vector3.new(0, 3, 0))
      end },
    { name = "copy position", desc = "puts your xyz on the clipboard as a teleport-ready string",
      source = [==[
local char = game:GetService("Players").LocalPlayer.Character
local root = char and char:FindFirstChild("HumanoidRootPart")
if not root then return end
local p = root.Position
local text = string.format("%.1f, %.1f, %.1f", p.X, p.Y, p.Z)
if setclipboard then setclipboard(text) end
print("position: " .. text)
]==],
      code = function()
        local char = game:GetService("Players").LocalPlayer.Character
        local root = char and char:FindFirstChild("HumanoidRootPart")
        if not root then return end
        local p = root.Position
        local text = string.format("%.1f, %.1f, %.1f", p.X, p.Y, p.Z)
        if setclipboard then setclipboard(text) end
        print("position: " .. text)
      end },
    { name = "fullbright", desc = "removes darkness without touching your other lighting settings",
      source = [==[
local Lighting = game:GetService("Lighting")

if _G.__umbraBright then
    for prop, value in pairs(_G.__umbraBright) do Lighting[prop] = value end
    _G.__umbraBright = nil
else
    _G.__umbraBright = {
        Ambient = Lighting.Ambient,
        OutdoorAmbient = Lighting.OutdoorAmbient,
        Brightness = Lighting.Brightness,
        FogEnd = Lighting.FogEnd,
    }
    Lighting.Ambient = Color3.fromRGB(180, 180, 180)
    Lighting.OutdoorAmbient = Color3.fromRGB(180, 180, 180)
    Lighting.Brightness = 3
    Lighting.FogEnd = 1e6
end
]==],
      code = function()
        local Lighting = game:GetService("Lighting")

        if _G.__umbraBright then
            for prop, value in pairs(_G.__umbraBright) do Lighting[prop] = value end
            _G.__umbraBright = nil
        else
            _G.__umbraBright = {
                Ambient = Lighting.Ambient,
                OutdoorAmbient = Lighting.OutdoorAmbient,
                Brightness = Lighting.Brightness,
                FogEnd = Lighting.FogEnd,
            }
            Lighting.Ambient = Color3.fromRGB(180, 180, 180)
            Lighting.OutdoorAmbient = Color3.fromRGB(180, 180, 180)
            Lighting.Brightness = 3
            Lighting.FogEnd = 1e6
        end
      end },
    { name = "free cam", desc = "detaches the camera from your body. WASD to fly, load again to attach",
      source = [==[
local Workspace = game:GetService("Workspace")
local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")

if _G.__umbraFreecam then
    _G.__umbraFreecam:Disconnect()
    _G.__umbraFreecam = nil
    Workspace.CurrentCamera.CameraType = Enum.CameraType.Custom
else
    local camera = Workspace.CurrentCamera
    local speed = 60
    camera.CameraType = Enum.CameraType.Scriptable
    _G.__umbraFreecam = RunService.RenderStepped:Connect(function(dt)
        local direction = Vector3.zero
        if UserInputService:IsKeyDown(Enum.KeyCode.W) then direction += camera.CFrame.LookVector end
        if UserInputService:IsKeyDown(Enum.KeyCode.S) then direction -= camera.CFrame.LookVector end
        if UserInputService:IsKeyDown(Enum.KeyCode.A) then direction -= camera.CFrame.RightVector end
        if UserInputService:IsKeyDown(Enum.KeyCode.D) then direction += camera.CFrame.RightVector end
        if direction.Magnitude > 0 then
            camera.CFrame = camera.CFrame + direction.Unit * speed * dt
        end
    end)
end
]==],
      code = function()
        local Workspace = game:GetService("Workspace")
        local RunService = game:GetService("RunService")
        local UserInputService = game:GetService("UserInputService")

        if _G.__umbraFreecam then
            _G.__umbraFreecam:Disconnect()
            _G.__umbraFreecam = nil
            Workspace.CurrentCamera.CameraType = Enum.CameraType.Custom
        else
            local camera = Workspace.CurrentCamera
            local speed = 60
            camera.CameraType = Enum.CameraType.Scriptable
            _G.__umbraFreecam = RunService.RenderStepped:Connect(function(dt)
                local direction = Vector3.zero
                if UserInputService:IsKeyDown(Enum.KeyCode.W) then direction += camera.CFrame.LookVector end
                if UserInputService:IsKeyDown(Enum.KeyCode.S) then direction -= camera.CFrame.LookVector end
                if UserInputService:IsKeyDown(Enum.KeyCode.A) then direction -= camera.CFrame.RightVector end
                if UserInputService:IsKeyDown(Enum.KeyCode.D) then direction += camera.CFrame.RightVector end
                if direction.Magnitude > 0 then
                    camera.CFrame = camera.CFrame + direction.Unit * speed * dt
                end
            end)
        end
      end },
    { name = "spin", desc = "rotates you on the spot, load again to stop",
      source = [==[
local RunService = game:GetService("RunService")

if _G.__umbraSpin then
    _G.__umbraSpin:Disconnect()
    _G.__umbraSpin = nil
else
    _G.__umbraSpin = RunService.RenderStepped:Connect(function(dt)
        local char = game:GetService("Players").LocalPlayer.Character
        local root = char and char:FindFirstChild("HumanoidRootPart")
        if root then root.CFrame = root.CFrame * CFrame.Angles(0, dt * 6, 0) end
    end)
end
]==],
      code = function()
        local RunService = game:GetService("RunService")

        if _G.__umbraSpin then
            _G.__umbraSpin:Disconnect()
            _G.__umbraSpin = nil
        else
            _G.__umbraSpin = RunService.RenderStepped:Connect(function(dt)
                local char = game:GetService("Players").LocalPlayer.Character
                local root = char and char:FindFirstChild("HumanoidRootPart")
                if root then root.CFrame = root.CFrame * CFrame.Angles(0, dt * 6, 0) end
            end)
        end
      end },
    { name = "unlock fps", desc = "lifts the frame cap. menu reads fps back in the status line",
      source = [==[
if setfpscap then
    setfpscap(240)
    print("fps cap lifted to 240")
else
    print("setfpscap missing: this needs the executor, not a LocalScript")
end
]==],
      code = function()
        if setfpscap then
            setfpscap(240)
            print("fps cap lifted to 240")
        else
            print("setfpscap missing: this needs the executor, not a LocalScript")
        end
      end },
    { name = "rejoin", desc = "comes back to the same place. needs the executor",
      source = [==[
local placeId = game.PlaceId
if queue_on_teleport then queue_on_teleport("print('rejoined')") end
if game:GetService("TeleportService") then
    pcall(function()
        game:GetService("TeleportService"):Teleport(placeId)
    end)
end
]==],
      code = function()
        local placeId = game.PlaceId
        if queue_on_teleport then queue_on_teleport("print('rejoined')") end
        if game:GetService("TeleportService") then
            pcall(function()
                game:GetService("TeleportService"):Teleport(placeId)
            end)
        end
      end },
}
-- [[LIBRARY-END]]

local playerPage = page("Player")
slider(playerPage, "walkspeed", 8, 200, state.speed, function(value)
    state.speed = value
    applySpeed()
end)
slider(playerPage, "jumppower", 50, 300, state.jump, function(value)
    state.jump = value
    applyJump()
end)
toggle(playerPage, "infinite jump", false, function(on) state.infJump = on end)
toggle(playerPage, "noclip", false, function(on) state.noClip = on end)
local heal = button(playerPage, "heal to full", 28)
track(heal.MouseButton1Click.Connect(function()
    local char = character()
    if char then char:FindFirstChildOfClass("Humanoid").Health = 1000 end
end))

local visualsPage = page("Visuals")
toggle(visualsPage, "fullbright", false, setFullbright)
toggle(visualsPage, "boxes", false, function(on)
    state.boxes = on
    if not on then clearEsp() end
end)
toggle(visualsPage, "names", false, function(on) state.names = on end)

local worldPage = page("World")
toggle(worldPage, "no fog", false, function(on)
    if on then
        state.restored.fog = Lighting.FogEnd
        Lighting.FogEnd = 1e5
    else
        Lighting.FogEnd = state.restored.fog or 100000
    end
end)
local tp = button(worldPage, "copy position", 28)
track(tp.MouseButton1Click.Connect(function()
    local char = character()
    if not char then return end
    local position = char:GetPivot().Position
    setclipboard(string.format("%.1f, %.1f, %.1f", position.X, position.Y, position.Z))
    statusLabel.Text = "copied"
end))

local scriptPage = page("Scripts")

local function note(text, good)
    label(console, text, 11, good and GOOD or Color3.fromRGB(255, 93, 108))
end

local search = Instance.new("TextBox")
search.Size = UDim2.new(1, 0, 0, 26)
search.BackgroundColor3 = ROW
search.BorderSizePixel = 0
search.PlaceholderText = "search scripts"
search.PlaceholderColor3 = DIM
search.Text = ""
search.TextSize = 12
search.TextColor3 = TEXT
search.ClearTextOnFocus = false
search.Font = Enum.Font.Gotham
search.TextXAlignment = Enum.TextXAlignment.Left
search.Parent = scriptPage
corner(search, 6)
pad(search, 8)

local list = Instance.new("ScrollingFrame")
list.Size = UDim2.new(1, 0, 0, 168)
list.BackgroundColor3 = ROW
list.BorderSizePixel = 0
list.ScrollBarThickness = 3
list.CanvasSize = UDim2.new(0, 0, 0, 0)
list.AutomaticCanvasSize = Enum.AutomaticSize.Y
list.Parent = scriptPage
corner(list, 6)

local listLayout = Instance.new("UIListLayout")
listLayout.Padding = UDim.new(0, 6)
listLayout.SortOrder = Enum.SortOrder.LayoutOrder
listLayout.Parent = list
pad(list, 6)

local editor = Instance.new("TextBox")
editor.Size = UDim2.new(1, 0, 0, 104)
editor.BackgroundColor3 = ROW
editor.BorderSizePixel = 0
editor.Text = ""
editor.PlaceholderText = "editor: write or pick a script"
editor.PlaceholderColor3 = DIM
editor.TextColor3 = TEXT
editor.TextSize = 12
editor.Font = Enum.Font.Code
editor.TextXAlignment = Enum.TextXAlignment.Left
editor.TextYAlignment = Enum.TextYAlignment.Top
editor.ClearTextOnFocus = false
editor.MultiLine = true
editor.Parent = scriptPage
corner(editor, 6)
pad(editor, 8)

local console = Instance.new("ScrollingFrame")
console.Size = UDim2.new(1, 0, 1, -382)
console.BackgroundColor3 = ROW
console.BorderSizePixel = 0
console.ScrollBarThickness = 3
console.CanvasSize = UDim2.new(0, 0, 0, 0)
console.AutomaticCanvasSize = Enum.AutomaticSize.Y
console.Parent = scriptPage
corner(console, 6)

local consoleLayout = Instance.new("UIListLayout")
consoleLayout.Padding = UDim.new(0, 3)
consoleLayout.Parent = console
pad(console, 8)

local function runSource(text, label_)
    local chunk, err = loadstring(text)
    if not chunk then
        note(label_ .. ": " .. tostring(err))
        return
    end
    local ok, result = pcall(chunk)
    note(label_ .. (ok and ": ran" or ": " .. tostring(result)), ok)
end

for _, entry in ipairs(LIBRARY) do
    local rowFrame = Instance.new("Frame")
    rowFrame.Size = UDim2.new(1, -12, 0, 40)
    rowFrame.BackgroundColor3 = PANEL
    rowFrame.BorderSizePixel = 0
    rowFrame.Parent = list
    corner(rowFrame, 6)

    local titleLabel = label(rowFrame, entry.name, 12, TEXT, true)
    titleLabel.Position = UDim2.new(0, 8, 0, 5)
    titleLabel.Size = UDim2.new(1, -86, 0, 14)

    local descLabel = label(rowFrame, entry.desc, 10, DIM)
    descLabel.Position = UDim2.new(0, 8, 0, 21)
    descLabel.Size = UDim2.new(1, -86, 0, 12)
    descLabel.TextTruncate = Enum.TextTruncate.AtEnd

    local loadBtn = Instance.new("TextButton")
    loadBtn.Size = UDim2.new(0, 34, 0, 22)
    loadBtn.Position = UDim2.new(1, -74, 0, 9)
    loadBtn.BackgroundColor3 = ACCENT
    loadBtn.Text = "load"
    loadBtn.TextSize = 11
    loadBtn.TextColor3 = TEXT
    loadBtn.Font = Enum.Font.GothamBold
    loadBtn.Parent = rowFrame
    corner(loadBtn, 5)

    hover(loadBtn, ACCENT, Color3.fromRGB(148, 120, 255))

    local editBtn = Instance.new("TextButton")
    editBtn.Size = UDim2.new(0, 30, 0, 22)
    editBtn.Position = UDim2.new(1, -36, 0, 9)
    editBtn.BackgroundColor3 = ROW
    editBtn.Text = "edit"
    editBtn.TextSize = 10
    editBtn.TextColor3 = DIM
    editBtn.Font = Enum.Font.Gotham
    editBtn.Parent = rowFrame
    corner(editBtn, 5)

    track(loadBtn.MouseButton1Click.Connect(function()
        local ok, result = pcall(entry.code)
        note(entry.name .. (ok and ": ran" or ": " .. tostring(result)), ok)
        toast(ok and (entry.name .. " loaded") or (entry.name .. " failed"), ok)
    end))
    track(editBtn.MouseButton1Click.Connect(function()
        editor.Text = entry.source
        editor:CaptureFocus()
    end))
    entry.frame = rowFrame
    entry.title = titleLabel
    entry.desc = descLabel
end

track(search:GetPropertyChangedSignal("Text"):Connect(function()
    local needle = string.lower(search.Text)
    for _, entry in ipairs(LIBRARY) do
        local haystack = string.lower(entry.name .. " " .. entry.desc)
        entry.frame.Visible = needle == "" or string.find(haystack, needle, 1, true) ~= nil
    end
end))

local runRow = Instance.new("Frame")
runRow.Size = UDim2.new(1, 0, 0, 26)
runRow.BackgroundTransparency = 1
runRow.Parent = scriptPage

local runButton = button(runRow, "run editor script", 26)
runButton.Size = UDim2.new(0, 130, 0, 26)
track(runButton.MouseButton1Click.Connect(function()
    if editor.Text == "" then
        note("editor empty")
        return
    end
    runSource(editor.Text, "editor")
end))

local copyButton = button(runRow, "copy", 26)
copyButton.Size = UDim2.new(0, 52, 0, 26)
copyButton.Position = UDim2.new(0, 138, 0, 0)
track(copyButton.MouseButton1Click.Connect(function()
    if setclipboard and editor.Text ~= "" then
        setclipboard(editor.Text)
        statusLabel.Text = "copied"
    end
end))

-- drag + toggle -------------------------------------------------------

do
    local dragging, start, origin
    track(titleBar.InputBegan:Connect(function(input)
        if input.UserInputType ~= Enum.UserInputType.MouseButton1 then return end
        dragging = true
        start = input.Position
        origin = root.Position
    end))
    track(UserInputService.InputEnded:Connect(function(input)
        if input.UserInputType == Enum.UserInputType.MouseButton1 then dragging = false end
    end))
    track(UserInputService.InputChanged:Connect(function(input)
        if not dragging or input.UserInputType ~= Enum.UserInputType.MouseMovement then return end
        local delta = input.Position - start
        root.Position = UDim2.new(origin.X.Scale, origin.X.Offset + delta.X,
            origin.Y.Scale, origin.Y.Offset + delta.Y)
    end))
end

track(UserInputService.InputBegan:Connect(function(input, typing)
    if typing then return end
    if input.KeyCode == Enum.KeyCode.RightShift then
        if shown then
            hide()
            statusLabel.Text = ""
        else
            show()
            statusLabel.Text = "idle"
        end
    end
end))

pages.Player.Visible = true
buttons.Player.BackgroundColor3 = ACCENT
buttons.Player.TextColor3 = TEXT
show()

pcall(function()
    StarterGui:SetCore("SendNotification", {
        Title = "Umbra",
        Text = "menu loaded, RightShift toggles",
        Duration = 4,
    })
end)

return {
    state = state,
    gui = gui,
    unload = function()
        for _, connection in connections do connection:Disconnect() end
        clearEsp()
        gui:Destroy()
    end,
}
