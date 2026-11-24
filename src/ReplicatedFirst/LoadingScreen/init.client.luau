--!nonstrict
--[[
	init.client.luau

	Drop this folder (LoadingScreen) into ReplicatedFirst and edit CONFIG below.
	Being a LocalScript in ReplicatedFirst means Roblox replaces its default
	loading screen with this one automatically.
]]

local ReplicatedFirst = game:GetService("ReplicatedFirst")
local ContentProvider = game:GetService("ContentProvider")
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

local Loader = require(script.Loader)

local CONFIG = {
	title = "YOUR GAME",
	tagline = "Building the world",
	tips = {
		"You can rebind most keys in Settings.",
		"Playing with sound on is recommended.",
		"Report bugs from the in-game menu.",
		"Your progress saves automatically.",
	},
	accent = Color3.fromRGB(124, 92, 255),
	accentEnd = Color3.fromRGB(0, 209, 255),
	background = Color3.fromRGB(10, 10, 14),
	logoImage = "", -- optional, e.g. "rbxassetid://123456789"
	minDisplayTime = 1.5,
	maxAssets = 250,
}

local player = Players.LocalPlayer
local loader = Loader.new(CONFIG)

-- Our frame is already on screen at this point, so the default screen can go.
ReplicatedFirst:RemoveDefaultLoadingScreen()
RunService.RenderStepped:Wait()

--[[ Collects things worth preloading. Add the attribute SkipLoading to any
	instance you want the loader to ignore. ]]
local function collectAssets()
	local assets = {}
	local seen = {}

	for _, instance in ipairs(game:GetDescendants()) do
		if #assets >= CONFIG.maxAssets then
			break
		end

		local contentId
		if instance:IsA("Sound") then
			contentId = instance.SoundId
		elseif instance:IsA("Decal") or instance:IsA("Texture") then
			contentId = instance.Texture
		elseif instance:IsA("ImageLabel") or instance:IsA("ImageButton") then
			contentId = instance.Image
		end

		if
			contentId
			and contentId ~= ""
			and not seen[contentId]
			and instance:GetAttribute("SkipLoading") ~= true
		then
			seen[contentId] = true
			table.insert(assets, instance)
		end
	end

	return assets
end

--- Preloads in batches so the bar can actually move while it works.
local function preload(assets)
	local total = #assets
	if total == 0 then
		return
	end

	local batchSize = 10
	local loaded = 0

	for startIndex = 1, total, batchSize do
		local stopIndex = math.min(startIndex + batchSize - 1, total)
		local batch = table.move(assets, startIndex, stopIndex, 1, {})

		pcall(ContentProvider.PreloadAsync, ContentProvider, batch)

		loaded = math.min(loaded + #batch, total)
		loader:SetProgress(
			0.08 + (loaded / total) * 0.84,
			string.format("Loading assets  %d/%d", loaded, total)
		)
	end
end

local function boot()
	loader:SetProgress(0.05, "Preparing world")
	preload(collectAssets())

	loader:SetProgress(0.94, "Syncing with server")
	if not game:IsLoaded() then
		game.Loaded:Wait()
	end

	if not player.Character then
		player.CharacterAdded:Wait()
	end

	loader:SetProgress(0.99, "Entering")
	task.wait(0.15)
end

-- Never leave a player staring at a frozen loading screen.
local success, err = pcall(boot)
if not success then
	warn("[Loader] " .. tostring(err))
end

loader:Finish("Ready")
