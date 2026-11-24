--!nocheck
-- Umbra menu. Single file, no dependencies, no loadstring.
-- Toggle with RightShift. Drag with the title bar. Config is per-place.

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")
local Lighting = game:GetService("Lighting")
local Workspace = game:GetService("Workspace")
local StarterGui = game:GetService("StarterGui")

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
track(closeButton.MouseButton1Click.Connect(function()
    root.Visible = false
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
local editor = Instance.new("TextBox")
editor.Size = UDim2.new(1, 0, 0, 96)
editor.BackgroundColor3 = ROW
editor.BorderSizePixel = 0
editor.Text = ""
editor.PlaceholderText = "print(\"hi\")"
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
console.Size = UDim2.new(1, 0, 1, -132)
console.BackgroundColor3 = ROW
console.BorderSizePixel = 0
console.ScrollBarThickness = 4
console.CanvasSize = UDim2.new(0, 0, 0, 0)
console.AutomaticCanvasSize = Enum.AutomaticSize.Y
console.Parent = scriptPage
corner(console, 6)

local consoleLayout = Instance.new("UIListLayout")
consoleLayout.Padding = UDim.new(0, 3)
consoleLayout.Parent = console
pad(console, 8)

local runButton = button(scriptPage, "run editor script", 28)
track(runButton.MouseButton1Click.Connect(function()
    local text = editor.Text
    if not text or text == "" then
        statusLabel.Text = "editor empty"
        return
    end
    local chunk, err = loadstring(text)
    if not chunk then
        label(console, err, 11, Color3.fromRGB(255, 93, 108))
        return
    end
    local ok, result = pcall(chunk)
    label(console, ok and "ok" or tostring(result), 11,
        ok and GOOD or Color3.fromRGB(255, 93, 108))
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
        root.Visible = not root.Visible
        statusLabel.Text = root.Visible and "idle" or ""
    end
end))

pages.Player.Visible = true
buttons.Player.BackgroundColor3 = ACCENT
buttons.Player.TextColor3 = TEXT
root.Visible = true

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
