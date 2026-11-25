-- name: noclip
-- desc: walk through walls. load again to turn off
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

if _G.__lyphenNoclip then
    _G.__lyphenNoclip:Disconnect()
    _G.__lyphenNoclip = nil
else
    _G.__lyphenNoclip = RunService.Stepped:Connect(function()
        local char = Players.LocalPlayer.Character
        if not char then return end
        for _, part in ipairs(char:GetDescendants()) do
            if part:IsA("BasePart") then part.CanCollide = false end
        end
    end)
end
