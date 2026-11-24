--!nonstrict
--[[
	Loader.luau

	A drop-in animated loading screen for Roblox.

		local Loader = require(script.Loader)
		local loader = Loader.new({
			title = "MY GAME",
			subtitle = "Loading world",
			tips = { "Tip number one.", "Tip number two." },
			accent = Color3.fromRGB(124, 92, 255),
			accentEnd = Color3.fromRGB(0, 209, 255),
			minDisplayTime = 1.5,
		})

		loader:SetProgress(0.4, "Loading assets")
		loader:Finish()   -- fills to 100%, holds for minDisplayTime, then fades out
		loader:Destroy()  -- instant removal

	No external assets are used, so it cannot fail to load art.
]]

local RunService = game:GetService("RunService")
local TweenService = game:GetService("TweenService")
local Players = game:GetService("Players")

local DEFAULT_TIPS = {
	"You can rebind most keys in Settings.",
	"Playing with sound on is recommended.",
	"If the game feels heavy, lower Graphics Quality in Settings.",
	"Your progress saves automatically.",
	"Report bugs from the in-game menu.",
}

local Loader = {}
Loader.__index = Loader

local function create(className, properties, parent)
	local instance = Instance.new(className)
	for key, value in pairs(properties) do
		instance[key] = value
	end
	instance.Parent = parent
	return instance
end

local function rounded(instance, radius)
	create("UICorner", { CornerRadius = UDim.new(0, radius) }, instance)
	return instance
end

local function label(properties, parent)
	local instance = create("TextLabel", properties, parent)
	instance.BackgroundTransparency = 1
	instance.BorderSizePixel = 0
	return instance
end

local function clampTextSize(instance, maxSize)
	create("UITextSizeConstraint", { MaxTextSize = maxSize, MinTextSize = 1 }, instance)
end

function Loader.new(config)
	config = config or {}

	local self = setmetatable({}, Loader)

	local accent = config.accent or Color3.fromRGB(124, 92, 255)
	local accentEnd = config.accentEnd or Color3.fromRGB(0, 209, 255)
	local background = config.background or Color3.fromRGB(10, 10, 14)
	local tips = config.tips or DEFAULT_TIPS

	self._target = 0
	self._current = 0
	self._statusBase = config.subtitle or "Loading"
	self._minDisplay = config.minDisplayTime or 1.5
	self._startedAt = os.clock()
	self._lastPercent = -1
	self._dotClock = 0
	self._dots = 0
	self._dead = false
	self._connections = {}
	self._tweens = {}

	local function track(connection)
		table.insert(self._connections, connection)
	end

	local playerGui = Players.LocalPlayer:WaitForChild("PlayerGui")

	----------------------------------------------------------------
	-- Screen + background
	----------------------------------------------------------------

	local gui = create("ScreenGui", {
		Name = "LoadingScreen",
		IgnoreGuiInset = true,
		ResetOnSpawn = false,
		ZIndexBehavior = Enum.ZIndexBehavior.Sibling,
		DisplayOrder = 1000,
	}, playerGui)

	local canvas = create("CanvasGroup", {
		Name = "Canvas",
		BackgroundColor3 = background,
		BorderSizePixel = 0,
		Size = UDim2.fromScale(1, 1),
		GroupTransparency = 0,
	}, gui)

	-- Soft accent wash so the flat background does not look dead.
	local wash = create("Frame", {
		Name = "Wash",
		BackgroundColor3 = accent,
		BackgroundTransparency = 0,
		BorderSizePixel = 0,
		Size = UDim2.fromScale(1, 1),
	}, canvas)

	create("UIGradient", {
		Rotation = 125,
		Color = ColorSequence.new(accent, background),
		Transparency = NumberSequence.new({
			NumberSequenceKeypoint.new(0, 0.84),
			NumberSequenceKeypoint.new(0.55, 0.97),
			NumberSequenceKeypoint.new(1, 1),
		}),
	}, wash)

	----------------------------------------------------------------
	-- Content stack
	----------------------------------------------------------------

	local content = create("Frame", {
		Name = "Content",
		BackgroundTransparency = 1,
		AnchorPoint = Vector2.new(0.5, 0.5),
		Position = UDim2.fromScale(0.5, 0.46),
		Size = UDim2.new(0, 520, 0, 0),
		AutomaticSize = Enum.AutomaticSize.Y,
	}, canvas)

	create("UIListLayout", {
		FillDirection = Enum.FillDirection.Vertical,
		HorizontalAlignment = Enum.HorizontalAlignment.Center,
		SortOrder = Enum.SortOrder.LayoutOrder,
		Padding = UDim.new(0, 12),
	}, content)

	local sizeScale = create("UIScale", { Scale = 1 }, content)

	-- Optional logo, otherwise a short accent marker above the title.
	if config.logoImage and config.logoImage ~= "" then
		local logo = create("ImageLabel", {
			Name = "Logo",
			BackgroundTransparency = 1,
			Image = config.logoImage,
			Size = UDim2.fromOffset(96, 96),
			LayoutOrder = 1,
		}, content)
		logo.ScaleType = Enum.ScaleType.Fit
	else
		local mark = create("Frame", {
			Name = "Mark",
			BackgroundColor3 = Color3.new(1, 1, 1),
			BorderSizePixel = 0,
			Size = UDim2.fromOffset(40, 3),
			LayoutOrder = 1,
		}, content)
		rounded(mark, 4)
		create("UIGradient", {
			Color = ColorSequence.new(accent, accentEnd),
		}, mark)
	end

	local title = label({
		Name = "Title",
		Size = UDim2.new(1, 0, 0, 52),
		Font = Enum.Font.GothamBold,
		Text = string.upper(config.title or "LOADING"),
		TextColor3 = Color3.new(1, 1, 1),
		TextScaled = true,
		TextTruncate = Enum.TextTruncate.AtEnd,
		LayoutOrder = 2,
	}, content)
	clampTextSize(title, 46)
	create("UIGradient", {
		Color = ColorSequence.new(Color3.new(1, 1, 1), accentEnd:Lerp(Color3.new(1, 1, 1), 0.55)),
	}, title)

	local subtitle = label({
		Name = "Subtitle",
		Size = UDim2.new(1, 0, 0, 20),
		Font = Enum.Font.GothamMedium,
		Text = config.tagline or "",
		TextColor3 = Color3.fromRGB(150, 150, 170),
		TextScaled = true,
		TextTruncate = Enum.TextTruncate.AtEnd,
		LayoutOrder = 3,
	}, content)
	clampTextSize(subtitle, 16)

	----------------------------------------------------------------
	-- Progress bar
	----------------------------------------------------------------

	local bar = create("Frame", {
		Name = "Bar",
		BackgroundTransparency = 1,
		Size = UDim2.new(1, 0, 0, 6),
		LayoutOrder = 5,
	}, content)

	local track = create("Frame", {
		Name = "Track",
		BackgroundColor3 = Color3.new(1, 1, 1),
		BackgroundTransparency = 0.9,
		BorderSizePixel = 0,
		Size = UDim2.fromScale(1, 1),
	}, bar)
	rounded(track, 6)

	local fill = create("Frame", {
		Name = "Fill",
		BackgroundColor3 = Color3.new(1, 1, 1),
		BorderSizePixel = 0,
		Size = UDim2.fromScale(0, 1),
		ClipsDescendants = true,
	}, track)
	rounded(fill, 6)

	create("UIGradient", {
		Color = ColorSequence.new(accent, accentEnd),
	}, fill)

	local shimmer = create("Frame", {
		Name = "Shimmer",
		BackgroundColor3 = Color3.new(1, 1, 1),
		BackgroundTransparency = 0.55,
		BorderSizePixel = 0,
		Size = UDim2.fromScale(0.35, 1),
		Position = UDim2.fromScale(-0.6, 0),
	}, fill)
	create("UIGradient", {
		Color = ColorSequence.new(Color3.new(1, 1, 1), Color3.new(1, 1, 1)),
		Transparency = NumberSequence.new({
			NumberSequenceKeypoint.new(0, 1),
			NumberSequenceKeypoint.new(0.5, 0.35),
			NumberSequenceKeypoint.new(1, 1),
		}),
	}, shimmer)

	local shimmerTween = TweenService:Create(
		shimmer,
		TweenInfo.new(1.8, Enum.EasingStyle.Linear, Enum.EasingDirection.Out, -1),
		{ Position = UDim2.fromScale(1.3, 0) }
	)
	shimmerTween:Play()
	table.insert(self._tweens, shimmerTween)

	----------------------------------------------------------------
	-- Status row
	----------------------------------------------------------------

	local statusRow = create("Frame", {
		Name = "Status",
		BackgroundTransparency = 1,
		Size = UDim2.new(1, 0, 0, 18),
		LayoutOrder = 6,
	}, content)

	local statusLabel = label({
		Name = "StatusText",
		Size = UDim2.fromScale(0.75, 1),
		Font = Enum.Font.Gotham,
		Text = self._statusBase,
		TextColor3 = Color3.fromRGB(150, 150, 170),
		TextSize = 13,
		TextXAlignment = Enum.TextXAlignment.Left,
	}, statusRow)

	local percentLabel = label({
		Name = "Percent",
		Size = UDim2.fromScale(0.25, 1),
		AnchorPoint = Vector2.new(1, 0),
		Position = UDim2.fromScale(1, 0),
		Font = Enum.Font.GothamMedium,
		Text = "0%",
		TextColor3 = accentEnd,
		TextSize = 13,
		TextXAlignment = Enum.TextXAlignment.Right,
	}, statusRow)

	----------------------------------------------------------------
	-- Tip line
	----------------------------------------------------------------

	local tipLabel
	if #tips > 0 then
		tipLabel = label({
			Name = "Tip",
			Size = UDim2.new(0, 640, 0, 44),
			AnchorPoint = Vector2.new(0.5, 0),
			Position = UDim2.new(0.5, 0, 1, -56),
			Font = Enum.Font.Gotham,
			TextColor3 = Color3.fromRGB(140, 140, 160),
			TextSize = 14,
			RichText = true,
			TextXAlignment = Enum.TextXAlignment.Center,
			TextYAlignment = Enum.TextYAlignment.Center,
			TextWrapped = true,
		}, canvas)

		local tipScale = create("UIScale", { Scale = 1 }, tipLabel)

		local function formatTip(text)
			return string.format('<font color="#%s"><b>TIP</b></font>   %s', accent:ToHex(), text)
		end

		tipLabel.Text = formatTip(tips[1])

		if #tips > 1 then
			task.spawn(function()
				local index = 2
				while not self._dead do
					task.wait(6)
					if self._dead then
						break
					end
					tipLabel.Text = formatTip(tips[index])
					index = index % #tips + 1
				end
			end)
		end

		-- Keep the tip line proportional on small screens.
		local function updateScale()
			local width = canvas.AbsoluteSize.X
			if width > 0 then
				tipScale.Scale = math.clamp(width / 1000, 0.6, 1)
			end
		end
		updateScale()
		track(canvas:GetPropertyChangedSignal("AbsoluteSize"):Connect(updateScale))
	end

	----------------------------------------------------------------
	-- Responsive scale for the main stack
	----------------------------------------------------------------

	local function updateContentScale()
		local width = canvas.AbsoluteSize.X
		if width > 0 then
			sizeScale.Scale = math.clamp(width / 1000, 0.62, 1)
		end
	end
	updateContentScale()
	track(canvas:GetPropertyChangedSignal("AbsoluteSize"):Connect(updateContentScale))

	----------------------------------------------------------------
	-- Per-frame smoothing of the bar
	----------------------------------------------------------------

	track(RunService.RenderStepped:Connect(function(dt)
		if self._dead then
			return
		end

		local difference = self._target - self._current
		if math.abs(difference) < 0.0005 then
			self._current = self._target
		else
			self._current = self._current + difference * math.min(1, dt * 7)
		end

		fill.Size = UDim2.fromScale(self._current, 1)

		local percent = math.floor(self._current * 100 + 0.5)
		if percent ~= self._lastPercent then
			self._lastPercent = percent
			percentLabel.Text = string.format("%d%%", percent)
		end

		-- Animated ellipsis keeps the screen alive while nothing is loading.
		self._dotClock = self._dotClock + dt
		if self._dotClock >= 0.4 then
			self._dotClock = 0
			if self._target < 0.999 then
				self._dots = self._dots % 3 + 1
				statusLabel.Text = self._statusBase .. string.rep(".", self._dots)
			else
				statusLabel.Text = self._statusBase
			end
		end
	end))

	self._gui = gui
	self._canvas = canvas
	self._fill = fill
	self._statusLabel = statusLabel
	return self
end

--- Sets the fill target (0-1) and optionally the status line.
function Loader:SetProgress(value, status)
	if self._dead then
		return
	end
	if status then
		self._statusBase = status
	end
	if value then
		self._target = math.max(self._current, math.clamp(value, 0, 1))
	end
end

--- Status line only.
function Loader:SetStatus(text)
	self:SetProgress(nil, text)
end

--- Fills to 100%, holds for minDisplayTime, then fades out.
function Loader:Finish(status)
	if self._dead then
		return
	end

	self:SetProgress(1, status or self._statusBase)

	local elapsed = os.clock() - self._startedAt
	if elapsed < self._minDisplay then
		task.wait(self._minDisplay - elapsed)
	end

	-- Give the bar a moment to visually reach the end.
	local timeout = os.clock() + 1
	while self._current < 0.995 and os.clock() < timeout do
		task.wait()
	end
	self._current = 1
	self._fill.Size = UDim2.fromScale(1, 1)

	self:_remove(0.45)
end

--- Removes the screen immediately.
function Loader:Destroy()
	self:_remove(0)
end

function Loader:_remove(fadeTime)
	if self._dead then
		return
	end
	self._dead = true

	for _, connection in ipairs(self._connections) do
		connection:Disconnect()
	end
	table.clear(self._connections)

	if fadeTime and fadeTime > 0 then
		local fade = TweenService:Create(
			self._canvas,
			TweenInfo.new(fadeTime, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut),
			{ GroupTransparency = 1 }
		)
		fade:Play()
		fade.Completed:Wait()
	end

	for _, tween in ipairs(self._tweens) do
		tween:Cancel()
	end
	table.clear(self._tweens)

	if self._gui then
		self._gui:Destroy()
	end
end

return Loader
