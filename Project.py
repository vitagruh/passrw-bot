num = int(input())
c = num % 10
b = (num % 100) // 10
a = num // 100
print(a*100+b*10+c)
print(a*100+c*10+b)
print(b*100+a*10+c)
print(b*100+c*10+a)
print(c*100+a*10+b)
print(c*100+b*10+a)