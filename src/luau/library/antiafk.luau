-- name: anti afk
-- desc: nudges the client so the 20 minute idle kick never fires
local VirtualUser = game:GetService("VirtualUser")
local player = game:GetService("Players").LocalPlayer

if _G.__lyphenAfk then _G.__lyphenAfk:Disconnect() end
_G.__lyphenAfk = player.Idled:Connect(function()
    VirtualUser:CaptureController()
    VirtualUser:ClickButton2(Vector2.new())
end)
