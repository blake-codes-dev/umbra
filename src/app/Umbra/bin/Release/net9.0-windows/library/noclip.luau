-- name: noclip
-- desc: walk through walls. load again to turn off
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
