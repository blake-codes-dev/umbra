-- name: teleport to cursor
-- desc: puts you where the mouse points, in front of the camera
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
