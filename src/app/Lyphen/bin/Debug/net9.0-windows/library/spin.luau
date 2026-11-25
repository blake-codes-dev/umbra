-- name: spin
-- desc: rotates you on the spot, load again to stop
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
